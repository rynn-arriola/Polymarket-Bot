"""Repair 'cancelled' rows that actually FILLED and RESOLVED on the exchange.

The 2026-07-14 mis-cancel class: confirm_fills checked a just-placed order
before the exchange's read path had indexed it, got NOT_FOUND on both the
order and the portfolio, and marked the row cancelled — but the order went
on to fill and resolve. Those settled results are invisible to reporting
(the summary W/L mismatch vs the Polymarket History tab).

Run ON THE SERVER (live DB + API access):
    python3 repair_miscancelled.py            # dry-run: show what would change
    python3 repair_miscancelled.py --apply    # write the repairs

For every live=1 cancelled row, asks the exchange for a POSITION_RESOLUTION
activity on that market. An account only gets a resolution activity for a
position it actually held, so a match means the row was mis-cancelled.
Repairs status/pnl/settled_at from the exchange's own fee-inclusive figure
(pnl_reconciled=1) and stake from the exchange cost basis. Skips resolutions
younger than RESOLUTION_STABLE_MINUTES — the exchange restates cost (fees)
shortly after posting, and an early read sticks wrong forever.
"""

import sqlite3
import sys
from datetime import datetime, timezone

import config

APPLY = "--apply" in sys.argv

from polymarket_us import PolymarketUS
client = PolymarketUS(key_id=config.KEY_ID, secret_key=config.SECRET_KEY)

con = sqlite3.connect("positions.db" if APPLY
                      else "file:positions.db?mode=ro", uri=not APPLY)
rows = con.execute(
    "SELECT id, market_slug, status, stake, quantity FROM positions "
    "WHERE live=1 AND status='cancelled'"
).fetchall()
print(f"{len(rows)} live cancelled row(s) to check "
      f"({'APPLY' if APPLY else 'dry-run — pass --apply to write'})\n")

repaired = skipped_unstable = 0
for pid, slug, status, stake, quantity in rows:
    try:
        resp = client.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"],
            "limit": 1,
        })
    except Exception as e:
        print(f"  {slug}: activity fetch failed ({e}) — skipped")
        continue
    activities = (resp.get("activities") or []) if isinstance(resp, dict) else []
    if not activities:
        continue  # genuinely never held — cancelled is correct
    pr = activities[0].get("positionResolution") or {}
    before = pr.get("beforePosition") or {}
    try:
        cost = float((before.get("cost") or {}).get("value"))
        cash_value = float((before.get("cashValue") or {}).get("value"))
    except (TypeError, ValueError):
        print(f"  {slug}: unrecognized resolution format — skipped: {before}")
        continue

    raw_ts = pr.get("updateTime") or activities[0].get("updateTime")
    settled_at = None
    if raw_ts:
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            if age_min < getattr(config, "RESOLUTION_STABLE_MINUTES", 45):
                print(f"  {slug}: resolution only {age_min:.0f} min old — fee "
                      f"restatement may be pending, re-run later")
                skipped_unstable += 1
                continue
            settled_at = ts.isoformat()
        except (ValueError, TypeError):
            pass
    if settled_at is None:
        settled_at = datetime.now(timezone.utc).isoformat()

    pnl = round(cash_value - cost, 2)
    new_status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
    print(f"  {slug}:")
    print(f"    cancelled -> {new_status}  pnl={pnl:+.2f}  settled_at={settled_at}")
    print(f"    stake {stake} -> {round(cost, 2)} (exchange cost basis)  "
          f"[beforePosition: {before}]")
    if APPLY:
        con.execute(
            "UPDATE positions SET status=?, pnl=?, settled_at=?, "
            "pnl_reconciled=1, stake=? WHERE id=?",
            (new_status, pnl, settled_at, round(cost, 2), pid),
        )
        con.commit()
        print("    WRITTEN")
    repaired += 1

con.close()
print(f"\n{repaired} row(s) {'repaired' if APPLY else 'would be repaired'}"
      + (f", {skipped_unstable} skipped as unstable (re-run in ~1h)"
         if skipped_unstable else ""))
if not APPLY and repaired:
    print("Re-run with --apply to write, then verify: python3 audit_reporting.py")
