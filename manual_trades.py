"""Manual trade ledger.

This is separate from the live bot's order/settlement loop. It records bets
you placed by hand, cashouts, cancelled manual orders, and final results in a
dedicated `manual_trades` table inside positions.db.

Examples:
    python manual_trades.py add --slug aec-... --sport CS2 --matchup "A vs B" --side "A" --price 0.42 --quantity 10
    python manual_trades.py cashout 1 --close-price 0.55
    python manual_trades.py close 1 --status won
    python manual_trades.py cancel 2
    python manual_trades.py list
    python manual_trades.py report
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from db import db, db_init


OPEN_STATUSES = {"pending", "open"}
CLOSED_STATUSES = {"won", "lost", "push", "cashed_out", "cancelled"}
ALL_STATUSES = sorted(OPEN_STATUSES | CLOSED_STATUSES)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(v) -> str:
    if v is None:
        return "--"
    v = float(v)
    return f"+${v:.2f}" if v > 0 else f"-${abs(v):.2f}" if v < 0 else "$0.00"


def _trade(trade_id: int):
    rows = db(
        """SELECT id, price, quantity, stake, status FROM manual_trades WHERE id=?""",
        (trade_id,),
        fetch=True,
    )
    if not rows:
        raise SystemExit(f"manual trade #{trade_id} not found")
    return rows[0]


def _computed_pnl(status: str, price: float, quantity: float, stake: float,
                  close_price: float | None, pnl: float | None) -> float | None:
    if pnl is not None:
        return round(float(pnl), 2)
    if status == "won":
        return round(quantity * (1.0 - price), 2)
    if status in ("lost", "cancelled"):
        return round(0.0 if status == "cancelled" else -stake, 2)
    if status == "push":
        return 0.0
    if status == "cashed_out" and close_price is not None:
        return round(quantity * close_price - stake, 2)
    return None


def cmd_add(args):
    db_init()
    now = utc_now()
    stake = args.stake if args.stake is not None else round(args.price * args.quantity, 2)
    closed = args.status in CLOSED_STATUSES
    pnl = _computed_pnl(args.status, args.price, args.quantity, stake, None, args.pnl)
    db(
        """INSERT INTO manual_trades
           (created_at, updated_at, market_slug, matchup, sport, side, price,
            quantity, stake, live, order_id, status, closed_at, pnl, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now, now, args.slug, args.matchup, args.sport.upper(), args.side,
            args.price, args.quantity, stake, 1 if args.live else 0,
            args.order_id, args.status, now if closed else None, pnl, args.notes,
        ),
    )
    trade_id = db("SELECT id FROM manual_trades ORDER BY id DESC LIMIT 1", fetch=True)[0][0]
    print(f"added manual trade #{trade_id}: {args.sport.upper()} {args.side} "
          f"{args.quantity:g} @ {args.price:.2f}, stake ${stake:.2f}, status={args.status}")


def cmd_list(args):
    db_init()
    filters = []
    params = []
    if args.status:
        filters.append("status=?")
        params.append(args.status)
    if args.sport:
        filters.append("sport=?")
        params.append(args.sport.upper())
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = db(
        f"""SELECT id, created_at, sport, side, price, quantity, stake, status,
                   pnl, market_slug, matchup
            FROM manual_trades {where}
            ORDER BY id DESC LIMIT ?""",
        (*params, args.limit),
        fetch=True,
    )
    if not rows:
        print("no manual trades found")
        return
    print(f"{'id':>4} {'date':<10} {'sport':<8} {'status':<10} {'side':<22} "
          f"{'qty':>7} {'price':>6} {'stake':>8} {'pnl':>9}  market/matchup")
    for trade_id, created, sport, side, price, qty, stake, status, pnl, slug, matchup in rows:
        label = matchup or slug or ""
        print(f"{trade_id:>4} {str(created)[:10]:<10} {str(sport or ''):<8} "
              f"{str(status or ''):<10} {str(side or '')[:22]:<22} "
              f"{float(qty or 0):>7.2f} {float(price or 0):>6.2f} "
              f"{float(stake or 0):>8.2f} {money(pnl):>9}  {label}")


def cmd_show(args):
    db_init()
    rows = db("SELECT * FROM manual_trades WHERE id=?", (args.id,), fetch=True)
    if not rows:
        raise SystemExit(f"manual trade #{args.id} not found")
    cols = [r[1] for r in db("PRAGMA table_info(manual_trades)", fetch=True)]
    for name, value in zip(cols, rows[0]):
        print(f"{name}: {value}")


def _close_trade(trade_id: int, status: str, close_price: float | None,
                 pnl: float | None, reason: str | None, notes: str | None):
    db_init()
    _id, price, quantity, stake, old_status = _trade(trade_id)
    if old_status in CLOSED_STATUSES:
        raise SystemExit(f"manual trade #{trade_id} is already {old_status}")
    price = float(price or 0)
    quantity = float(quantity or 0)
    stake = float(stake or price * quantity)
    final_pnl = _computed_pnl(status, price, quantity, stake, close_price, pnl)
    if status == "cashed_out" and final_pnl is None:
        raise SystemExit("cashout needs --pnl or --close-price")
    now = utc_now()
    db(
        """UPDATE manual_trades
           SET status=?, close_price=?, pnl=?, close_reason=?, notes=?,
               closed_at=?, updated_at=?
           WHERE id=?""",
        (status, close_price, final_pnl, reason, notes, now, now, trade_id),
    )
    try:
        import reporting
        reporting.post_discord_manual_trade(trade_id)
        reporting.post_discord_summary("Manual trade updated")
    except Exception:
        # Manual tracking must still record locally if Discord is down or not
        # configured. reporting.py logs webhook failures itself in bot mode.
        pass
    print(f"manual trade #{trade_id} marked {status}; pnl={money(final_pnl)}")


def cmd_close(args):
    _close_trade(args.id, args.status, args.close_price, args.pnl, args.reason, args.notes)


def cmd_cashout(args):
    _close_trade(args.id, "cashed_out", args.close_price, args.pnl, "cashout", args.notes)


def cmd_cancel(args):
    _close_trade(args.id, "cancelled", None, args.pnl, args.reason or "cancelled", args.notes)


def cmd_sync(_args):
    """Force an immediate reconcile of every open manual trade (and detect
    hand cash-outs of bot positions) against the exchange — the same pass the
    bot loop runs every MANUAL_SYNC_INTERVAL_MIN. Run this ON THE SERVER: it
    needs the live positions.db and exchange auth."""
    db_init()
    import config
    import manual_sync
    from polymarket_us import PolymarketUS
    if not getattr(config, "LIVE", False):
        print("config.LIVE is False — manual sync only makes sense against the live account")
        return
    auth = PolymarketUS(key_id=config.KEY_ID, secret_key=config.SECRET_KEY)
    cashed, closed = manual_sync.detect_manual_cashouts(auth), manual_sync.sync_open_manual_trades(auth)
    print(f"bot positions cashed out by hand: {len(cashed)}")
    print(f"manual trades closed this pass:   {closed}")
    print("done — review with: python manual_trades.py list")


def cmd_report(_args):
    db_init()
    total = db(
        """SELECT COUNT(*),
                  COALESCE(SUM(CASE WHEN status IN ('won','lost','push','cashed_out')
                                    THEN pnl ELSE 0 END), 0),
                  COALESCE(SUM(stake), 0)
           FROM manual_trades""",
        fetch=True,
    )[0]
    print(f"Manual trades: {total[0]} total | P&L {money(total[1])} | stake ${float(total[2] or 0):.2f}")
    print()
    rows = db(
        """SELECT status, COUNT(*), COALESCE(SUM(pnl),0)
           FROM manual_trades GROUP BY status ORDER BY status""",
        fetch=True,
    )
    print("By status:")
    for status, n, pnl in rows:
        print(f"  {status:<10} {n:>4}  {money(pnl):>9}")
    rows = db(
        """SELECT sport, COUNT(*), COALESCE(SUM(CASE WHEN status IN ('won','lost','push','cashed_out')
                                                    THEN pnl ELSE 0 END),0)
           FROM manual_trades GROUP BY sport ORDER BY COUNT(*) DESC""",
        fetch=True,
    )
    if rows:
        print("\nBy sport:")
        for sport, n, pnl in rows:
            print(f"  {str(sport or 'UNKNOWN'):<10} {n:>4}  {money(pnl):>9}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Track manually placed Polymarket trades.")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="record a manually placed trade")
    add.add_argument("--slug", required=True, help="Polymarket market slug")
    add.add_argument("--sport", required=True, help="sport/market group label, e.g. CS2")
    add.add_argument("--side", required=True, help="pick/side you bought")
    add.add_argument("--price", required=True, type=float, help="entry price, e.g. 0.42")
    add.add_argument("--quantity", required=True, type=float, help="shares/contracts filled")
    add.add_argument("--stake", type=float, help="cash staked; defaults to price * quantity")
    add.add_argument("--matchup", default="", help="human-readable matchup")
    add.add_argument("--order-id", default="", help="optional exchange order id")
    add.add_argument("--status", choices=ALL_STATUSES, default="open")
    add.add_argument("--pnl", type=float, help="pnl for an already-closed manual trade")
    add.add_argument("--notes", default="")
    add.add_argument("--dry-run", action="store_false", dest="live", help="mark as simulated, not real")
    add.set_defaults(func=cmd_add, live=True)

    ls = sub.add_parser("list", help="list manual trades")
    ls.add_argument("--status", choices=ALL_STATUSES)
    ls.add_argument("--sport")
    ls.add_argument("--limit", type=int, default=25)
    ls.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one manual trade")
    show.add_argument("id", type=int)
    show.set_defaults(func=cmd_show)

    close = sub.add_parser("close", help="mark a manual trade won/lost/push/cashed_out/cancelled")
    close.add_argument("id", type=int)
    close.add_argument("--status", required=True, choices=sorted(CLOSED_STATUSES))
    close.add_argument("--pnl", type=float, help="explicit P&L; otherwise won/lost/push can be computed")
    close.add_argument("--close-price", type=float, help="cashout price, used to compute cashout P&L")
    close.add_argument("--reason", default="")
    close.add_argument("--notes", default="")
    close.set_defaults(func=cmd_close)

    cash = sub.add_parser("cashout", help="mark a manual trade cashed out")
    cash.add_argument("id", type=int)
    cash.add_argument("--pnl", type=float, help="explicit P&L")
    cash.add_argument("--close-price", type=float, help="cashout price, computes P&L as close_price*quantity - stake")
    cash.add_argument("--notes", default="")
    cash.set_defaults(func=cmd_cashout)

    cancel = sub.add_parser("cancel", help="mark a manual trade cancelled")
    cancel.add_argument("id", type=int)
    cancel.add_argument("--pnl", type=float, default=0.0)
    cancel.add_argument("--reason", default="")
    cancel.add_argument("--notes", default="")
    cancel.set_defaults(func=cmd_cancel)

    report = sub.add_parser("report", help="manual trade summary")
    report.set_defaults(func=cmd_report)

    sync = sub.add_parser("sync", help="reconcile open manual trades + bot cash-outs "
                                       "against the exchange now (run on the server)")
    sync.set_defaults(func=cmd_sync)
    return p


def main():
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
