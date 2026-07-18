"""Position sizing and risk gate — same categories of protection as the
existing bot.py (max open positions, per-sport daily cap, one-position-per-
event, daily loss halt, bankroll commitment, whole-contract limit orders),
reimplemented against this project's own config and database.
"""

import json
import logging

import config
from db import day_open_balance, db, period_bounds_utc

log = logging.getLogger("divergence_bot.risk")

_UNTRACKED_SLUGS_THIS_RUN: set[str] = set()


def _max_stake(effective_bankroll: float) -> float:
    """Per-bet cap as a percent of bankroll, but never below MIN_STAKE (so a
    small account can still place one whole-contract bet)."""
    return max(config.MIN_STAKE, effective_bankroll * getattr(config, "MAX_STAKE_PCT", 0.02))


def stake_for(effective_bankroll: float) -> float:
    """Flat sizing: STAKE_PCT of current capital, clamped to
    [MIN_STAKE, MAX_STAKE_PCT * bankroll]."""
    stake = effective_bankroll * config.STAKE_PCT
    return round(max(config.MIN_STAKE, min(_max_stake(effective_bankroll), stake)), 2)


def kelly_stake(effective_bankroll: float, price: float, prob: float) -> float:
    """Fractional-Kelly sizing: stake more on bigger edges. Buying a contract
    at `price` that pays $1 with probability `prob` has payout odds
    b = (1-price)/price; full Kelly bets bankroll * (prob - (1-prob)/b).
    Full Kelly is famously too aggressive when probabilities are estimates
    (and ours are), so this scales by config.KELLY_FRACTION (default 0.25).
    Clamped to the same [MIN_STAKE, MAX_STAKE_PCT * bankroll] limits as flat
    sizing; KELLY_FRACTION = 0 falls back to flat sizing entirely."""
    fraction = getattr(config, "KELLY_FRACTION", 0.0)
    if fraction <= 0 or not (0 < price < 1) or not (0 < prob < 1):
        return stake_for(effective_bankroll)
    b = (1 - price) / price
    kelly = prob - (1 - prob) / b
    if kelly <= 0:
        return stake_for(effective_bankroll)
    stake = effective_bankroll * kelly * fraction
    return round(max(config.MIN_STAKE, min(_max_stake(effective_bankroll), stake)), 2)


def risk_check(sport: str, event_id: str, market_slug_: str,
                effective_bankroll: float, stake: float) -> str | None:
    """Return a rejection reason, or None if entry is allowed."""
    if market_slug_ in _UNTRACKED_SLUGS_THIS_RUN:
        return ("market blocked this run — a possibly-untracked order exists for it "
                "(DB insert failure, ambiguous order-create error, or untracked exchange "
                "state); reconcile manually to clear")

    # Every count/sum is scoped to the current mode (live vs dry-run) so
    # simulated positions in the DB can't count against real-money limits
    # (or vice versa) — e.g. leftover dry-run open positions must not eat the
    # live MAX_OPEN_POSITIONS budget or block a live entry on the same event.
    live = 1 if config.LIVE else 0

    open_count, rescheduled_open = db(
        """SELECT COUNT(*),
                  SUM(CASE WHEN status='open' AND rescheduled_at IS NOT NULL THEN 1 ELSE 0 END)
           FROM positions
           WHERE status IN ('pending','open') AND live=?""",
        (live,), fetch=True,
    )[0]
    rescheduled_open = rescheduled_open or 0
    effective_max_open = config.MAX_OPEN_POSITIONS + rescheduled_open
    if open_count >= effective_max_open:
        suffix = (f" + {rescheduled_open} rescheduled"
                  if rescheduled_open else "")
        return f"max open positions reached ({open_count}/{effective_max_open}{suffix})"

    day_start, day_end = period_bounds_utc("today")
    sport_today = db(
        """SELECT COUNT(*) FROM positions
           WHERE sport=? AND created_at >= ? AND created_at < ? AND status != 'cancelled' AND live=?""",
        (sport, day_start, day_end, live),
        fetch=True,
    )[0][0]
    if sport_today >= config.MAX_PER_SPORT_PER_DAY:
        return f"sport cap reached for {sport} today ({sport_today})"

    # Duplicate guard is NOT live-filtered — it must match the market_slug
    # UNIQUE constraint (one row per slug across ALL modes). If it were
    # live-scoped, a live entry could pass the check while an existing dry-run
    # row owns that slug; the order would place, the insert would fail the
    # UNIQUE constraint, and every rerun would re-place it (a duplicate order).
    # Skipping a market that already has ANY position is the safe behavior.
    dup = db(
        "SELECT COUNT(*) FROM positions WHERE market_slug=? OR event_id=?",
        (market_slug_, event_id),
        fetch=True,
    )[0][0]
    if dup:
        return "already have a position on this event"

    day_pnl = db(
        "SELECT COALESCE(SUM(pnl),0) FROM positions WHERE settled_at >= ? AND settled_at < ? AND live=?",
        (day_start, day_end, live),
        fetch=True,
    )[0][0]
    # persist=False: read-only lookup. effective_bankroll may be the cold-start
    # BANKROLL guess (balance API down) — the loss limit can use it for THIS
    # check, but it must never be written as the day's official snapshot.
    loss_baseline = day_open_balance(effective_bankroll, persist=False)
    loss_limit = loss_baseline * config.DAILY_LOSS_LIMIT_PCT
    if day_pnl <= -loss_limit:
        return (f"DAILY LOSS LIMIT hit ({day_pnl:.2f}, limit -{loss_limit:.2f} "
                f"= {config.DAILY_LOSS_LIMIT_PCT:.1%} of ${loss_baseline:.2f} day-open balance) — halted for today")

    committed = db(
        "SELECT COALESCE(SUM(stake),0) FROM positions WHERE status IN ('pending','open') AND live=?",
        (live,), fetch=True,
    )[0][0]
    fee_buffer = effective_bankroll * getattr(config, "FEE_BUFFER_PCT", 0.005)
    if committed + stake + fee_buffer > effective_bankroll:
        return (f"bankroll fully committed (balance ${effective_bankroll:.2f}, "
                f"stake ${stake:.2f}, fee buffer ${fee_buffer:.2f})")

    return None


def place_order(client, slug: str, side: str, ask: float, stake: float):
    """side: "long" or "short" — a divergence entry can be on either team,
    unlike bot.py which only ever bought the long side. Confirmed against the
    installed SDK's OrderIntent literal (polymarket_us/types/orders.py):
    ORDER_INTENT_BUY_LONG and ORDER_INTENT_BUY_SHORT both exist.

    Contracts are whole units on this exchange (see bot.py's test_preview.py
    finding: cashOrderQty is rejected on limit orders). floor() so we never
    request more than the stake actually covers."""
    intent = "ORDER_INTENT_BUY_LONG" if side == "long" else "ORDER_INTENT_BUY_SHORT"
    quantity = int(stake // ask)
    if not config.LIVE:
        log.info(f"[DRY-RUN] WOULD BUY {side.upper()} {quantity} @ {ask:.2f} on {slug} (${stake:.2f} cash)")
        return "DRY_RUN", quantity

    payload = {
        "marketSlug": slug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{ask:.2f}", "currency": "USD"},
        "quantity": quantity,
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    }
    order = client.orders.create(payload)
    try:
        order_id = order.get("orderId") or order.get("id") or json.dumps(order)[:60]
    except Exception:
        log.critical(f"UNTRACKED LIVE ORDER on {slug}: order was SENT but response "
                     f"unparseable. Raw: {str(order)[:300]} — CHECK THE APP MANUALLY.")
        raise
    log.info(f"[LIVE] ORDER PLACED {side.upper()} {quantity} @ {ask:.2f} on {slug} (order {order_id})")
    return str(order_id), quantity
