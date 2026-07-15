"""Manual exchange activity — the single reconciliation engine.

The operator trades by hand in the Polymarket app on the same account the bot
uses: standalone manual bets (longs AND shorts), adding fills to an existing
manual bet, cashing a bet out early, and cashing out the bot's own positions.
The exchange nets all of it silently; nothing tells the bot. Before this
module existed the importer read only the FIRST batch of fills, treated sells
as buys, and a position the operator had flattened waited forever for a
POSITION_RESOLUTION that never comes (the exchange only posts one for a
position actually held at resolution). All three failure modes are real,
observed on 2026-07-15 (fra-esp / eng-arg / wsh-tor).

Design rule: every pass RECOMPUTES from the exchange's full fill history and
overwrites — no incremental bookkeeping, so a crashed or repeated pass can
never double-count. The arithmetic was validated against the exchange's own
figures for all three incident markets before this shipped (cost basis
matches beforePosition.cost to the cent; realized P&L matches the cash that
hit the balance).

Fill-money semantics (verified 2026-07-15 against real fills):
  BUY  trade.cost  = cash OUT, fee-inclusive
  SELL trade.cost  = cash IN, already net of fee
so P&L needs no separate fee handling.

Shared by divergence_bot.py (per-cycle pass) and manual_trades.py (ad-hoc
`sync` subcommand). Imports only config/db/reporting — never the Elo stack.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import config
import reporting
from db import db

log = logging.getLogger("divergence_bot.manual_sync")

MANUAL = "MANUAL_ORDER_INDICATOR_MANUAL"

# Sentinel: the resolution check itself FAILED (feed down, rate-limited,
# unrecognized shape). Distinct from None = the feed answered and there is
# definitively no resolution. verify_cancelled_rows marked a mis-cancelled
# row (ica-dnt, 2026-07-14) verified-forever off one transient failure
# because the two cases were conflated — callers must retry on this value
# and never conclude anything from it.
RESOLUTION_CHECK_FAILED = object()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw):
    """ISO timestamp (Z or offset form) -> aware datetime, None if unusable."""
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _money_value(value) -> float:
    try:
        return float((value or {}).get("value") if isinstance(value, dict) else value)
    except (TypeError, ValueError):
        return 0.0


def resolution_pnl(auth, slug: str):
    """(P&L, stable) from the account's POSITION_RESOLUTION activity for this
    market; None when the feed answered and shows NO resolution (definitive);
    RESOLUTION_CHECK_FAILED when the check itself failed — retry later.
    Callers should branch with isinstance(res, tuple) for the settled case.

    `stable` is False while the activity is younger than
    RESOLUTION_STABLE_MINUTES: the exchange RESTATES the cost basis (rolls
    fees in) shortly after posting the resolution — 16 positions audited on
    2026-07-13 carried P&L read too early, overstating the book ~$10.6.
    Callers may act on an unstable figure (it's close, and Discord shouldn't
    wait) but must not mark it final (pnl_reconciled) until stable.

    This feed is the same one the app's History tab shows, and it publishes
    well BEFORE the public /markets/{slug}/settlement record — polling only
    the latter made settlement messages lag by hours (reported 2026-07-13)."""
    try:
        resp = auth.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"],
            "limit": 1,
        })
    except Exception as e:
        log.warning(f"Resolution-activity check failed for {slug}: {e}")
        return RESOLUTION_CHECK_FAILED
    if not isinstance(resp, dict) or "activities" not in resp:
        return RESOLUTION_CHECK_FAILED  # unexpected shape is NOT an empty feed
    if not resp["activities"]:
        return None
    activities = resp["activities"]
    activity = next((a for a in activities
                     if isinstance(a, dict) and isinstance(a.get("positionResolution"), dict)), None)
    if activity is None:
        return None  # the endpoint answered, but has no resolution for this market
    pr = activity["positionResolution"]
    before = pr.get("beforePosition") or {}
    try:
        cost = float((before.get("cost") or {}).get("value"))
        cash_value = float((before.get("cashValue") or {}).get("value"))
    except (TypeError, ValueError):
        log.warning(f"Unrecognized resolution activity format for {slug}")
        return RESOLUTION_CHECK_FAILED
    stable = True  # unknown age -> assume stable (pre-updateTime API shapes)
    raw_ts = pr.get("updateTime") or activity.get("updateTime")
    if raw_ts:
        ts = _parse_ts(raw_ts)
        if ts is not None:
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            stable = age_min >= getattr(config, "RESOLUTION_STABLE_MINUTES", 45)
    return round(cash_value - cost, 2), stable


def manual_fills(auth, slug: str, since=None):
    """Every MANUAL fill on a market, oldest first. None = fetch failed
    (conclude nothing, retry later); [] = feed answered, no manual fills.

    Bot fills carry MANUAL_ORDER_INDICATOR_AUTOMATIC and are excluded, which
    is what lets the same market carry a bot position AND the operator's own
    activity without cross-contamination. `since` (aware datetime or ISO
    string) drops fills at or before it — used to scope to a trading episode
    (fills newer than a bot entry, or than a previous closed manual row).

    Known gap: the fill's order metadata is read from the AGGRESSOR side of
    the trade. Every observed app order crossed the book (taker), so this has
    matched reality; a manual order that rested and filled passively could be
    missed. If a manual fill ever seems invisible, this is where to look."""
    try:
        resp = auth.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_TRADE"],
            "limit": 100,
        })
    except Exception as e:
        log.warning(f"Manual-fill fetch failed for {slug}: {e}")
        return None
    if not isinstance(resp, dict) or "activities" not in resp:
        return None
    if isinstance(since, str):
        since = _parse_ts(since)
    fills = []
    for activity in resp["activities"] or []:
        trade = activity.get("trade") if isinstance(activity, dict) else None
        if not isinstance(trade, dict):
            continue
        execution = trade.get("aggressorExecution") or {}
        order = execution.get("order") or trade.get("aggressor") or {}
        # The marketSlug re-check is not paranoia: the activities endpoint
        # returns account-level items (deposits, transfers) even when asked
        # for one market, so everything must be re-matched.
        if (order.get("marketSlug") != slug
                or order.get("manualOrderIndicator") != MANUAL):
            continue
        qty = _money_value(trade.get("qtyDecimal") or trade.get("qty")
                           or execution.get("lastShares"))
        cost = _money_value(trade.get("cost"))
        fee = _money_value(execution.get("commissionNotionalCollected"))
        if qty <= 0 or cost <= 0:
            continue
        created = _parse_ts(trade.get("createTime"))
        if since is not None and (created is None or created <= since):
            continue
        fills.append({
            "is_buy": order.get("side") == "ORDER_SIDE_BUY",
            "qty": qty,
            "cost": cost,          # buy: fee-incl cash out; sell: fee-net cash in
            "fee": fee,
            "order_id": str(order.get("id") or ""),
            "created": created,
            "created_raw": str(trade.get("createTime") or ""),
            "meta": order.get("marketMetadata") or trade.get("market") or {},
        })
    fills.sort(key=lambda f: f["created"] or datetime.min.replace(tzinfo=timezone.utc))
    return fills


def replay_fills(fills):
    """Average-cost replay of a chronological fill sequence.

    Returns (net_qty, open_cost, realized):
      net_qty   signed position: > 0 long, < 0 short (sell-to-open)
      open_cost cash at risk on what's still held (for a short: the net
                credit received), fee-inclusive — matches the exchange's
                beforePosition.cost bookkeeping
      realized  P&L banked by fills that reduced the position

    A fill that crosses through zero (sell more than held) is split: the
    reducing part realizes P&L, the excess opens the opposite direction at
    that fill's average price."""
    net = 0.0
    open_cost = 0.0
    realized = 0.0
    for f in fills:
        qty, cost = f["qty"], f["cost"]
        direction = 1.0 if f["is_buy"] else -1.0
        while qty > 1e-9:
            if net == 0 or (net > 0) == (direction > 0):
                # opening / adding
                net += direction * qty
                open_cost += cost
                qty = 0.0
            else:
                # reducing (possibly through zero)
                reduce_qty = min(qty, abs(net))
                fill_portion = cost * (reduce_qty / qty)
                basis_portion = open_cost * (reduce_qty / abs(net))
                if net > 0:   # long reduced by a sell: cash in minus basis out
                    realized += fill_portion - basis_portion
                else:         # short reduced by a buy: credit kept minus buyback
                    realized += basis_portion - fill_portion
                net += direction * reduce_qty
                open_cost -= basis_portion
                qty -= reduce_qty
                cost -= fill_portion
                if abs(net) < 1e-9:
                    net, open_cost = 0.0, 0.0
    return net, open_cost, realized


# ---------------------------------------------------------------- manual rows

def _episode_since(slug: str):
    """Fills belonging to a PREVIOUS, already-closed manual trade on the same
    market must not leak into a new row — scope a row's fill history to after
    the latest closed sibling."""
    rows = db(
        """SELECT MAX(closed_at) FROM manual_trades
           WHERE live=1 AND market_slug=? AND closed_at IS NOT NULL""",
        (slug,), fetch=True,
    )
    return rows[0][0] if rows and rows[0][0] else None


def import_manual_trade(auth, slug: str) -> bool:
    """Create a manual_trades row for a market the exchange shows activity on
    but nothing tracks. Numbers are recomputed by sync_manual_row every pass,
    so this only has to plant a correct skeleton."""
    fills = manual_fills(auth, slug, since=_episode_since(slug))
    if not fills:
        return False
    meta = fills[0]["meta"] or {}
    team = meta.get("team") or {}
    now = _utc_now_iso()
    db(
        """INSERT INTO manual_trades
           (created_at, updated_at, market_slug, matchup, sport, side, price,
            quantity, stake, live, order_id, status, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fills[0]["created_raw"] or now, now, slug,
         meta.get("title") or slug,
         str(team.get("league") or "manual").upper(),
         meta.get("outcome") or team.get("name") or "manual",
         0.0, 0.0, 0.0, 1, "", "open",
         "Auto-imported from exchange manual activity."),
    )
    row = db("SELECT id FROM manual_trades ORDER BY id DESC LIMIT 1", fetch=True)[0]
    log.warning(f"AUTO-IMPORTED manual exchange trade {slug} (#{row[0]})")
    sync_manual_row(auth, row[0])
    return True


def sync_manual_row(auth, trade_id: int, include_closed: bool = False):
    """True one manual_trades row up against the exchange. Returns
    'closed' / 'updated' / None (nothing to do or couldn't check).

    Idempotent: quantity/price/stake/direction are recomputed from the full
    manual-fill history each call, never incremented. Rows with no exchange
    fills (hand-entered hypotheticals) are left exactly as the human wrote
    them. Closes as:
      won/lost/push  — a POSITION_RESOLUTION exists (P&L = realized from any
                       partial exits + the exchange's own resolution figure),
                       only once the figure is fee-stable
      cashed_out     — the operator flattened the position with fills; no
                       resolution will ever post for it"""
    rows = db(
        """SELECT market_slug, status, quantity, stake, pnl, price,
                  COALESCE(is_long, 1)
           FROM manual_trades WHERE id=? AND live=1""",
        (trade_id,), fetch=True,
    )
    if not rows:
        return None
    slug, status, old_qty, old_stake, old_pnl, old_price, old_is_long = rows[0]
    if status not in ("open", "pending") and not include_closed:
        return None
    if not slug:
        return None
    open_siblings = db(
        """SELECT COUNT(*) FROM manual_trades
           WHERE live=1 AND market_slug=? AND status IN ('open','pending')""",
        (slug,), fetch=True,
    )[0][0]
    if open_siblings > 1:
        log.warning(f"manual sync skipped {slug}: {open_siblings} open manual rows "
                    f"share this market — fills can't be attributed; close the "
                    f"extras with manual_trades.py")
        return None
    fills = manual_fills(auth, slug, since=_episode_since(slug) if not include_closed else None)
    if fills is None:
        return None    # fetch failed — conclude nothing
    if not fills:
        return None    # hand-entered row with no exchange trace — human's word stands
    net, open_cost, realized = replay_fills(fills)
    now = _utc_now_iso()

    res = resolution_pnl(auth, slug)
    if isinstance(res, tuple) and abs(net) > 1e-6:
        pnl_res, stable = res
        if not stable:
            return None   # exchange still restating fees — close on a later pass
        pnl = round(realized + pnl_res, 2)
        new_status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
        db("""UPDATE manual_trades
              SET status=?, pnl=?, quantity=?, stake=?, price=?, is_long=?,
                  closed_at=?, updated_at=?, close_reason='exchange resolution'
              WHERE id=?""",
           (new_status, pnl, round(abs(net), 2), round(open_cost, 2),
            round(open_cost / abs(net), 4) if abs(net) > 1e-9 else old_price,
            1 if net > 0 else 0, now, now, trade_id))
        log.info(f"MANUAL TRADE SETTLED {new_status.upper()} {slug}  P&L {pnl:+.2f}")
        _post_card(trade_id, slug)
        return "closed"

    if abs(net) < 0.01:
        # Flattened by the operator's own fills. No resolution will ever post
        # for a flat position — waiting for one leaves the row open forever.
        pnl = round(realized, 2)
        last_exit = next((f for f in reversed(fills)), None)
        close_px = None
        if last_exit is not None and last_exit["qty"] > 0:
            close_px = round(last_exit["cost"] / last_exit["qty"], 4)
        db("""UPDATE manual_trades
              SET status='cashed_out', pnl=?, close_price=?, closed_at=?,
                  updated_at=?, close_reason='cashed out on exchange'
              WHERE id=?""",
           (pnl, close_px, now, now, trade_id))
        log.warning(f"MANUAL TRADE CASHED OUT {slug}  realized P&L {pnl:+.2f}")
        _post_card(trade_id, slug)
        return "closed"

    if status not in ("open", "pending"):
        return None   # closed row still holding on-exchange: human closed it early on purpose

    # Still held: true up size / direction / cost basis, bank partial exits.
    new_qty = round(abs(net), 2)
    new_stake = round(open_cost, 2)
    new_price = round(open_cost / abs(net), 4)
    new_is_long = 1 if net > 0 else 0
    new_pnl = round(realized, 2) if abs(realized) >= 0.01 else None
    changed = (abs((old_qty or 0) - new_qty) > 0.005
               or abs((old_stake or 0) - new_stake) > 0.005
               or (old_pnl is None) != (new_pnl is None)
               or abs((old_pnl or 0) - (new_pnl or 0)) > 0.005
               or int(old_is_long) != new_is_long)
    if not changed:
        return None
    db("""UPDATE manual_trades
          SET quantity=?, stake=?, price=?, is_long=?, pnl=?, updated_at=?
          WHERE id=?""",
       (new_qty, new_stake, new_price, new_is_long, new_pnl, now, trade_id))
    log.warning(f"MANUAL TRADE UPDATED {slug}: "
                f"{'long' if new_is_long else 'SHORT'} {new_qty:g} contracts, "
                f"at-risk ${new_stake:.2f}"
                + (f", realized so far {new_pnl:+.2f}" if new_pnl is not None else ""))
    _post_card(trade_id, slug)
    return "updated"


def _post_card(trade_id: int, slug: str) -> None:
    try:
        reporting.post_discord_manual_trade(trade_id)
    except Exception:
        log.exception(f"Manual-trade Discord card failed for {slug}")


def sync_open_manual_trades(auth) -> int:
    """One pass over every live open manual trade. Returns rows closed."""
    rows = db("SELECT id FROM manual_trades WHERE live=1 AND status IN ('open','pending')",
              fetch=True)
    closed = 0
    for (trade_id,) in rows:
        try:
            if sync_manual_row(auth, trade_id) == "closed":
                closed += 1
        except Exception:
            log.exception(f"manual sync failed for trade #{trade_id}")
    return closed


# ------------------------------------------------------- bot-position cashouts

def detect_manual_cashouts(auth) -> list[int]:
    """Find open BOT positions the operator has (partly) exited by hand and
    record it. Returns position ids fully closed this pass (for the
    settlement Discord flow).

    Direction-aware: a manual SELL reduces a long bot position; a manual BUY
    reduces (covers) a short one. The opposite direction — the operator ADDING
    to a bot position — is NOT split into a manual trade: the exchange's
    resolution figure will cover the combined position and splitting it would
    double-count, so it is recorded on the row and shouted about instead.

    Partial exits overwrite manual_sold_qty / cashout_pnl (recomputed from the
    full fill history — idempotent); settlement paths add cashout_pnl on top
    of the resolution figure for what was still held."""
    settled: list[int] = []
    rows = db(
        """SELECT id, market_slug, price, quantity, stake, COALESCE(is_long,1),
                  created_at, COALESCE(manual_sold_qty,0),
                  COALESCE(manual_added_qty,0), COALESCE(cashout_pnl,0)
           FROM positions WHERE live=1 AND status='open'""",
        fetch=True,
    )
    for (pid, slug, price, quantity, stake, is_long, created_at,
         old_sold, old_added, old_cashout) in rows:
        if not quantity or not stake:
            continue
        fills = manual_fills(auth, slug, since=created_at)
        if not fills:   # None (couldn't check) and [] (no manual activity) alike
            continue
        reduce_is_buy = not is_long   # covering a short is a BUY
        cost_per = stake / quantity   # bot's cash at risk per contract
        remaining = float(quantity)
        realized = 0.0
        sold = 0.0
        added = 0.0
        for f in fills:
            if f["is_buy"] == reduce_is_buy:
                take = min(f["qty"], remaining)
                if take <= 1e-9:
                    continue   # exits beyond the bot's size — untracked flip, warned below
                fill_portion = f["cost"] * (take / f["qty"])
                if is_long:
                    realized += fill_portion - take * cost_per
                else:
                    # short: buy back `take` contracts for fill_portion; the
                    # exchange releases $1/contract collateral less that cost
                    realized += take - fill_portion - take * cost_per
                remaining -= take
                sold += take
                if f["qty"] - take > 1e-6:
                    added += 0  # excess of a flipping fill handled by the warning below
            else:
                added += f["qty"]
        now = _utc_now_iso()
        if remaining < 0.5:
            pnl = round(realized, 2)
            new_status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
            db("""UPDATE positions
                  SET status=?, pnl=?, settled_at=?, pnl_reconciled=1,
                      manual_sold_qty=?, cashout_pnl=?, close_reason='manual_cashout'
                  WHERE id=?""",
               (new_status, pnl, now, round(sold, 2), pnl, pid))
            settled.append(pid)
            log.warning(f"MANUAL CASH-OUT: bot position {slug} fully exited by hand "
                        f"on the exchange — closed {new_status.upper()}, realized "
                        f"P&L {pnl:+.2f} (entry ${stake:.2f})")
            continue
        if sold > old_sold + 0.01 or abs(realized - old_cashout) > 0.01:
            db("""UPDATE positions
                  SET manual_sold_qty=?, cashout_pnl=?,
                      close_reason='partial_manual_cashout'
                  WHERE id=?""",
               (round(sold, 2), round(realized, 2), pid))
            log.warning(f"PARTIAL MANUAL CASH-OUT: {slug} — {sold:g} of {quantity:g} "
                        f"contracts exited by hand, {realized:+.2f} banked; "
                        f"{remaining:g} still riding to settlement")
        if added > old_added + 0.01:
            db("UPDATE positions SET manual_added_qty=? WHERE id=?",
               (round(added, 2), pid))
            log.warning(f"MANUAL ADDITION to bot position {slug}: {added:g} extra "
                        f"contracts bought by hand. They settle inside this "
                        f"position's P&L (the exchange nets them) — fine for the "
                        f"books, but it distorts this row's model stats. Prefer "
                        f"separate markets for manual bets.")
    return settled


# ----------------------------------------------------------------- loop pacing

_last_pass = 0.0


def manual_activity_pass(auth) -> tuple[list[int], int]:
    """Throttled per-cycle entry point for the bot loop: (bot position ids
    fully cashed out, manual rows closed). The extra API reads only run every
    MANUAL_SYNC_INTERVAL_MIN (default 5) minutes — a cash-out learned a few
    minutes late changes nothing (the exchange already executed it)."""
    global _last_pass
    if auth is None:
        return [], 0
    interval = getattr(config, "MANUAL_SYNC_INTERVAL_MIN", 5) * 60
    now = time.monotonic()
    if _last_pass and now - _last_pass < interval:
        return [], 0
    _last_pass = now
    cashed_out = detect_manual_cashouts(auth)
    closed = sync_open_manual_trades(auth)
    return cashed_out, closed
