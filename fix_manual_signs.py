"""One-off: re-sync manual cash-out rows whose P&L was booked with the sign
bug (2026-07-15). The old sync read the instrument-frame `order.side`, so
underdog bets (recorded as a "sell" of the favorite) had their P&L sign
inverted — three real LOSSES were booked as equal-size gains. The corrected
manual_sync uses the exchange's own `effectiveRealizedPnl`.

This reopens every live `cashed_out` manual row and re-runs the corrected
sync against the exchange, so each lands on the exchange's own realized
figure. Idempotent and safe: a cashed_out position is flat, so re-syncing
just recomputes P&L; a row that fails to re-close (e.g. exchange briefly
unreachable) is restored to its prior values and reported, never left
dangling.

Run ON THE SERVER (live DB + exchange auth):
    python3 fix_manual_signs.py            # dry-run: show what would change
    python3 fix_manual_signs.py --apply    # write the corrections
"""
import sys

import config
import manual_sync
from db import db, db_init
from polymarket_us import PolymarketUS

APPLY = "--apply" in sys.argv


def main():
    db_init()
    auth = PolymarketUS(key_id=config.KEY_ID, secret_key=config.SECRET_KEY)
    rows = db("SELECT id, market_slug, status, pnl FROM manual_trades "
              "WHERE live=1 AND status='cashed_out'", fetch=True)
    print(f"{len(rows)} cashed_out manual row(s) to re-sync "
          f"({'APPLY' if APPLY else 'dry-run — pass --apply to write'})\n")
    for tid, slug, status, old_pnl in rows:
        # Snapshot the current row so a failed re-sync can be rolled back.
        before = db("SELECT status, pnl, quantity, stake, price, is_long, "
                    "close_price, closed_at, close_reason, updated_at "
                    "FROM manual_trades WHERE id=?", (tid,), fetch=True)[0]
        if not APPLY:
            # Dry-run: compute what the corrected engine would produce without
            # touching the row — reopen in a throwaway way is unsafe, so just
            # read the exchange and show the target figure.
            fills = manual_sync.manual_fills(auth, slug)
            if not fills:
                print(f"  #{tid} {slug}: no manual fills found — skipped")
                continue
            _net, _oc, realized = manual_sync.summarize(fills)
            flag = "" if (old_pnl is not None and abs(old_pnl - realized) < 0.01) else "  <-- WOULD CHANGE"
            print(f"  #{tid} {slug}: pnl {old_pnl:+.2f} -> {realized:+.2f}{flag}")
            continue
        # Apply: reopen, re-sync, verify it re-closed; roll back on failure.
        db("UPDATE manual_trades SET status='open', pnl=NULL, closed_at=NULL, "
           "close_price=NULL WHERE id=?", (tid,))
        result = None
        try:
            result = manual_sync.sync_manual_row(auth, tid)
        except Exception as e:
            print(f"  #{tid} {slug}: re-sync raised ({e}) — rolling back")
        after = db("SELECT status, pnl FROM manual_trades WHERE id=?", (tid,), fetch=True)[0]
        if result == "closed" and after[0] in ("cashed_out", "won", "lost", "push"):
            print(f"  #{tid} {slug}: pnl {old_pnl:+.2f} -> {after[1]:+.2f}  ({after[0]}) WRITTEN")
        else:
            db("""UPDATE manual_trades SET status=?, pnl=?, quantity=?, stake=?,
                  price=?, is_long=?, close_price=?, closed_at=?, close_reason=?,
                  updated_at=? WHERE id=?""", (*before, tid))
            print(f"  #{tid} {slug}: re-sync did not close cleanly (result={result}) "
                  f"— restored to prior values, investigate")
    if not APPLY:
        print("\nRe-run with --apply to write, then verify: python3 audit_reporting.py")


if __name__ == "__main__":
    main()
