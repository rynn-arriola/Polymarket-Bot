"""Manual exchange activity — the single reconciliation engine.

The operator trades by hand in the Polymarket app on the same account the bot
uses: standalone manual bets (longs AND shorts), adding fills to an existing
bet, cashing a bet out early, and cashing out the bot's own positions. The
exchange nets all of it silently; nothing tells the bot.

DIRECTION IS IN THE INSTRUMENT FRAME, NOT YOUR FRAME (learned the hard way,
2026-07-15). A market has a canonical long side (the favorite/"YES") and a
short side (the underdog/"NO"). Backing the underdog — e.g. BUYING Toronto
when Toronto is the NO side — is recorded on the fill as `order.side =
ORDER_SIDE_SELL`. Reading that literal side inverted the P&L sign on every
underdog bet: three real cash-outs (fra-esp/eng-arg/wsh-tor) that were
LOSSES got booked as equal-size GAINS (+$132 shown vs −$544 real). Never use
`order.side` for the operator's economic direction. The trustworthy fields:

  order.action  ORDER_ACTION_BUY / ORDER_ACTION_SELL   -> the operator's own
                buy/sell of their chosen team (what they clicked)
  trade.effectiveRealizedPnl                            -> the EXCHANGE's own
                realized P&L on a closing fill, fee-exact, correctly signed.
                Absent on opening fills. This is the authority — we sum it
                rather than re-deriving P&L from fill directions ourselves.

So realized P&L never comes from our arithmetic anymore; it comes from the
exchange. We only use `action` to tell held-vs-flat and to label the entry.

Shared by divergence_bot.py (per-cycle pass) and manual_trades.py (ad-hoc
`sync` subcommand). Imports only config/db/reporting/api_guard — never the
Elo stack.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import api_guard
import config
import reporting
from db import db

log = logging.getLogger("divergence_bot.manual_sync")

MANUAL = "MANUAL_ORDER_INDICATOR_MANUAL"
ACTION_BUY = "ORDER_ACTION_BUY"
ACTION_SELL = "ORDER_ACTION_SELL"

# Sentinel: the resolution check itself FAILED (feed down, rate-limited,
# unrecognized shape). Distinct from None = the feed answered and there is
# definitively no resolution. verify_cancelled_rows marked a mis-cancelled
# row (ica-dnt, 2026-07-14) verified-forever off one transient failure
# because the two cases were conflated — callers must retry on this value
# and never conclude anything from it.
RESOLUTION_CHECK_FAILED = object()

EPS = 0.01  # contracts; below this a position is flat


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

    P&L here is cashValue - cost of the held-to-settlement position; it is
    side-independent (a short's cost basis and settlement value are both in
    the same frame) so it needs none of the action/effectiveRealizedPnl
    handling the trade path does.

    `stable` is False while the activity is younger than
    RESOLUTION_STABLE_MINUTES: the exchange RESTATES the cost basis (rolls
    fees in) shortly after posting the resolution — 16 positions audited on
    2026-07-13 carried P&L read too early, overstating the book ~$10.6.
    Callers may act on an unstable figure (it's close, and Discord shouldn't
    wait) but must not mark it final (pnl_reconciled) until stable."""
    try:
        resp = auth.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"],
            "limit": 1,
        })
    except Exception as e:
        api_guard.note_error(e)
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


def _order_of(trade: dict) -> dict:
    ex = trade.get("aggressorExecution") or {}
    return ex.get("order") or trade.get("aggressor") or {}


def manual_fills(auth, slug: str, since=None):
    """Every MANUAL fill on a market, oldest first. None = fetch failed
    (conclude nothing, retry later); [] = feed answered, no manual fills.

    Each fill carries the operator's TRUE direction (order.action, not the
    instrument-frame order.side) and the exchange's own realized P&L
    (trade.effectiveRealizedPnl, present only on closing fills). Bot fills
    carry MANUAL_ORDER_INDICATOR_AUTOMATIC and are excluded. `since` (aware
    datetime or ISO string) drops fills at or before it."""
    try:
        resp = auth.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_TRADE"],
            "limit": 100,
        })
    except Exception as e:
        api_guard.note_error(e)
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
        order = _order_of(trade)
        # The marketSlug re-check is not paranoia: the activities endpoint
        # returns account-level items (deposits, transfers) even when asked
        # for one market, so everything must be re-matched.
        if (order.get("marketSlug") != slug
                or order.get("manualOrderIndicator") != MANUAL):
            continue
        action = order.get("action")
        if action not in (ACTION_BUY, ACTION_SELL):
            # No trustworthy direction -> skip rather than guess (guessing is
            # exactly what caused the sign inversion).
            log.warning(f"Manual fill on {slug} has no order.action ({action!r}) — skipped")
            continue
        qty = _money_value(trade.get("qtyDecimal") or trade.get("qty"))
        cost = _money_value(trade.get("cost"))
        if qty <= 0 or cost <= 0:
            continue
        eff = trade.get("effectiveRealizedPnl")
        eff_realized = _money_value(eff) if eff else None
        intent = str(order.get("intent") or "")
        is_short = "SHORT" in intent or order.get("outcomeSide") == "OUTCOME_SIDE_NO"
        created = _parse_ts(trade.get("createTime"))
        if since is not None and (created is None or created <= since):
            continue
        fills.append({
            "is_buy": action == ACTION_BUY,   # operator's own buy/sell
            "qty": qty,
            "cost": cost,                      # cash magnitude of the fill (fee-incl)
            "eff_realized": eff_realized,      # exchange's realized P&L on a close
            "is_short": is_short,              # chosen team is the market's NO side
            "created": created,
            "created_raw": str(trade.get("createTime") or ""),
            "meta": order.get("marketMetadata") or trade.get("market") or {},
        })
    fills.sort(key=lambda f: f["created"] or datetime.min.replace(tzinfo=timezone.utc))
    return fills


def summarize(fills):
    """Reduce a chronological fill list to the current state, using the
    exchange's own realized figure — no direction arithmetic of our own.

    Returns (net, open_cost, realized):
      net       operator's chosen-team net contracts (BUY adds, SELL removes;
                > 0 long the team, < 0 net short it, ~0 flat)
      open_cost avg-cost basis of the still-held contracts (0 when flat)
      realized  Σ trade.effectiveRealizedPnl over closing fills — the
                exchange's own fee-exact, correctly-signed realized P&L
    """
    net = sum(f["qty"] if f["is_buy"] else -f["qty"] for f in fills)
    buy_qty = sum(f["qty"] for f in fills if f["is_buy"])
    buy_cost = sum(f["cost"] for f in fills if f["is_buy"])
    sell_qty = sum(f["qty"] for f in fills if not f["is_buy"])
    sell_cost = sum(f["cost"] for f in fills if not f["is_buy"])
    realized = round(sum(f["eff_realized"] for f in fills if f["eff_realized"] is not None), 2)
    if net > EPS and buy_qty > 0:            # net long the team: cost basis of held
        open_cost = round(buy_cost * (net / buy_qty), 2)
    elif net < -EPS and sell_qty > 0:        # net short the team: credit of held
        open_cost = round(sell_cost * (abs(net) / sell_qty), 2)
    else:
        open_cost = 0.0
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
            quantity, stake, live, order_id, status, is_long, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fills[0]["created_raw"] or now, now, slug,
         meta.get("title") or slug,
         str(team.get("league") or "manual").upper(),
         meta.get("outcome") or team.get("name") or "manual",
         0.0, 0.0, 0.0, 1, "", "open",
         0 if fills[0]["is_short"] else 1,
         "Auto-imported from exchange manual activity."),
    )
    row = db("SELECT id FROM manual_trades ORDER BY id DESC LIMIT 1", fetch=True)[0]
    log.warning(f"AUTO-IMPORTED manual exchange trade {slug} (#{row[0]})")
    sync_manual_row(auth, row[0])
    return True


def sync_manual_row(auth, trade_id: int):
    """True one manual_trades row up against the exchange. Returns
    'closed' / 'updated' / None (nothing to do or couldn't check).

    Idempotent: quantity/price/stake/pnl are recomputed from the full
    manual-fill history each call, never incremented. Rows with no exchange
    fills (hand-entered hypotheticals) are left as the human wrote them.
    P&L is the exchange's own (resolution figure and/or effectiveRealizedPnl),
    never our arithmetic. Closes as:
      won/lost/push  — a POSITION_RESOLUTION exists (held-to-settlement P&L
                       plus any realized from partial exits), once fee-stable
      cashed_out     — the operator flattened the position with fills; no
                       resolution will post for it, P&L = realized"""
    rows = db(
        """SELECT market_slug, status, quantity, stake, pnl, price,
                  COALESCE(is_long, 1)
           FROM manual_trades WHERE id=? AND live=1""",
        (trade_id,), fetch=True,
    )
    if not rows:
        return None
    slug, status, old_qty, old_stake, old_pnl, old_price, old_is_long = rows[0]
    if status not in ("open", "pending"):
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
    fills = manual_fills(auth, slug, since=_episode_since(slug))
    if fills is None:
        return None    # fetch failed — conclude nothing
    if not fills:
        return None    # hand-entered row with no exchange trace — human's word stands
    net, open_cost, realized = summarize(fills)
    is_long = 0 if fills[0]["is_short"] else 1
    now = _utc_now_iso()

    res = resolution_pnl(auth, slug)
    if isinstance(res, tuple) and abs(net) > EPS:
        # Position was HELD to settlement: exchange resolution P&L on the held
        # portion, plus anything realized from partial exits before it.
        pnl_res, stable = res
        if not stable:
            return None   # exchange still restating fees — close on a later pass
        pnl = round(realized + pnl_res, 2)
        new_status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
        held_qty = round(abs(net), 2)
        db("""UPDATE manual_trades
              SET status=?, pnl=?, quantity=?, stake=?, price=?, is_long=?,
                  closed_at=?, updated_at=?, close_reason='exchange resolution'
              WHERE id=?""",
           (new_status, pnl, held_qty, round(open_cost, 2),
            round(open_cost / held_qty, 4) if held_qty > EPS else old_price,
            is_long, now, now, trade_id))
        log.info(f"MANUAL TRADE SETTLED {new_status.upper()} {slug}  P&L {pnl:+.2f}")
        _post_card(trade_id, slug)
        return "closed"

    if abs(net) < EPS:
        # Flattened by the operator's own fills (cashed out). No resolution
        # will post for a flat position; P&L is the exchange's realized sum.
        # Show the ENTRY (what was bought/staked) so the card reads sanely,
        # with the realized loss/gain and the exit price.
        buy_qty = sum(f["qty"] for f in fills if f["is_buy"])
        buy_cost = sum(f["cost"] for f in fills if f["is_buy"])
        exits = [f for f in fills if not f["is_buy"]]
        exit_qty = sum(f["qty"] for f in exits)
        exit_cash = sum(f["cost"] for f in exits)
        close_px = round(exit_cash / exit_qty, 4) if exit_qty > EPS else None
        entry_qty = round(buy_qty, 2) if buy_qty > EPS else round(exit_qty, 2)
        entry_cost = round(buy_cost, 2) if buy_cost > EPS else round(exit_cash, 2)
        db("""UPDATE manual_trades
              SET status='cashed_out', pnl=?, quantity=?, stake=?, price=?,
                  is_long=?, close_price=?, closed_at=?, updated_at=?,
                  close_reason='cashed out on exchange'
              WHERE id=?""",
           (realized, entry_qty, entry_cost,
            round(entry_cost / entry_qty, 4) if entry_qty > EPS else old_price,
            is_long, close_px, now, now, trade_id))
        log.warning(f"MANUAL TRADE CASHED OUT {slug}  realized P&L {realized:+.2f}")
        _post_card(trade_id, slug)
        return "closed"

    # Still held: true up size / direction / cost basis, bank partial exits.
    new_qty = round(abs(net), 2)
    new_stake = round(open_cost, 2)
    new_price = round(open_cost / abs(net), 4)
    new_pnl = realized if abs(realized) >= EPS else None
    changed = (abs((old_qty or 0) - new_qty) > 0.005
               or abs((old_stake or 0) - new_stake) > 0.005
               or (old_pnl is None) != (new_pnl is None)
               or abs((old_pnl or 0) - (new_pnl or 0)) > 0.005
               or int(old_is_long) != is_long)
    if not changed:
        return None
    db("""UPDATE manual_trades
          SET quantity=?, stake=?, price=?, is_long=?, pnl=?, updated_at=?
          WHERE id=?""",
       (new_qty, new_stake, new_price, is_long, new_pnl, now, trade_id))
    log.warning(f"MANUAL TRADE UPDATED {slug}: "
                f"{'long' if is_long else 'SHORT'} {new_qty:g} contracts, "
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

    Direction-aware via the operator's own `action` (never the instrument
    side): a manual SELL reduces a long bot position; a manual BUY reduces
    (covers) a short one. The opposite — ADDING to a bot position by hand — is
    recorded and shouted about, not split, because the exchange's resolution
    figure will cover the combined position.

    Realized P&L on the exit is the exchange's own effectiveRealizedPnl on
    those fills (fee-exact, correctly signed, computed against the position's
    real cost basis) — not our arithmetic. manual_sold_qty / cashout_pnl are
    recomputed from the full fill history each pass (idempotent)."""
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
        # A fill REDUCES the bot position when the operator's action is opposite
        # the bot's side: bot long -> operator SELL reduces; bot short -> BUY.
        reduce_is_buy = not bool(is_long)
        remaining = float(quantity)
        realized = 0.0
        sold = 0.0
        added = 0.0
        for f in fills:
            if f["is_buy"] == reduce_is_buy:
                take = min(f["qty"], remaining)
                if take <= 1e-9:
                    continue   # exits beyond the bot's size — flip, warned below
                # Exchange's own realized on this closing fill (already correctly
                # signed and fee-exact); prorate if the fill only partly reduces.
                eff = f["eff_realized"]
                if eff is not None:
                    realized += eff * (take / f["qty"])
                remaining -= take
                sold += take
            else:
                added += f["qty"]
        now = _utc_now_iso()
        if remaining < 0.5 and sold > EPS:
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
        if sold > old_sold + EPS or abs(round(realized, 2) - old_cashout) > EPS:
            db("""UPDATE positions
                  SET manual_sold_qty=?, cashout_pnl=?,
                      close_reason='partial_manual_cashout'
                  WHERE id=?""",
               (round(sold, 2), round(realized, 2), pid))
            log.warning(f"PARTIAL MANUAL CASH-OUT: {slug} — {sold:g} of {quantity:g} "
                        f"contracts exited by hand, {realized:+.2f} banked; "
                        f"{remaining:g} still riding to settlement")
        if added > old_added + EPS:
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
