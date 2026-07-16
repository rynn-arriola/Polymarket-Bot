"""Reporting-integrity audit: verifies every invariant the Discord reports
depend on, in the DB and against the exchange. Read-only — changes nothing.

Run ON THE SERVER (live DB + API access):  python3 audit_reporting.py

Checks:
  A. DB invariants reporting assumes (status/pnl consistency, NULLs in
     columns the stats read, timestamp formats, stuck rows).
  B. Exchange cross-check: every recent POSITION_RESOLUTION vs the DB —
     missed settlements (the 2026-07-13 mis-cancel class) and P&L drift.
  C. For any P&L mismatch: dump ALL resolution activities for that market
     (a market posting MULTIPLE resolutions would explain reconcile
     landing on a wrong figure — suspected from lol-sdm-fue 2026-07-12).
"""

import sqlite3
import sys

import config

FLAGS = 0


def flag(msg):
    global FLAGS
    FLAGS += 1
    print(f"  [!] {msg}")


def section(title):
    print(f"\n=== {title} ===")


con = sqlite3.connect("file:positions.db?mode=ro", uri=True)
q = lambda sql, *a: con.execute(sql, a).fetchall()

# ------------------------------------------------------------------ A. DB
section("A1. Status distribution (live)")
for status, n in q("SELECT status, COUNT(*) FROM positions WHERE live=1 GROUP BY status"):
    print(f"  {status:<10} {n}")

section("A2. Settled rows missing pnl (would drop from P&L totals)")
rows = q("SELECT market_slug,status FROM positions WHERE live=1 AND status IN ('won','lost','push') AND pnl IS NULL")
for slug, status in rows:
    flag(f"{slug}: {status} but pnl NULL")
if not rows:
    print("  none — good")

section("A3. Non-settled rows CARRYING pnl (would be invisible losses/wins)")
rows = q("SELECT market_slug,status,pnl FROM positions WHERE live=1 AND status NOT IN ('won','lost','push') AND pnl IS NOT NULL")
for slug, status, pnl in rows:
    flag(f"{slug}: status={status} but pnl={pnl} — reporting now excludes it, but the row needs a decision")
if not rows:
    print("  none — good")

section("A4. NULLs in columns the stats tables read")
for col in ("created_at", "price", "stake", "quantity", "sport", "side"):
    n = q(f"SELECT COUNT(*) FROM positions WHERE live=1 AND {col} IS NULL")[0][0]
    (flag if n else print)(f"  {col}: {n} NULL" if n else f"  {col}: ok")

section("A5. Timestamp sanity (created_at/settled_at must be ISO, T-separated)")
for col in ("created_at", "settled_at"):
    n = q(f"SELECT COUNT(*) FROM positions WHERE live=1 AND {col} IS NOT NULL AND {col} NOT LIKE '____-__-__T%'")[0][0]
    (flag if n else print)(f"  {col}: {n} malformed" if n else f"  {col}: ok")

section("A6. Settled rows with settled_at missing (period queries need it)")
n = q("SELECT COUNT(*) FROM positions WHERE live=1 AND status IN ('won','lost','push') AND settled_at IS NULL")[0][0]
(flag if n else print)(f"  {n} settled rows without settled_at" if n else "  none — good")

section("A7. Rows pending > 1h (fill confirmation should resolve in minutes)")
rows = q("""SELECT market_slug, created_at FROM positions WHERE live=1 AND status='pending'
            AND created_at < strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-1 hour')""")
for slug, created in rows:
    flag(f"{slug}: pending since {created}")
if not rows:
    print("  none — good")

section("A8. Estimated pnl still unreconciled (should clear within a cycle or two)")
rows = q("""SELECT market_slug, pnl, settled_at FROM positions
            WHERE live=1 AND status IN ('won','lost','push') AND COALESCE(pnl_reconciled,0)=0""")
for slug, pnl, settled in rows:
    print(f"  {slug}: pnl={pnl} (estimate) settled {str(settled)[:16]}")
if not rows:
    print("  none — all settled pnl is exchange-reconciled")

# ------------------------------------------------------- B. exchange truth
section("B. Exchange resolutions vs DB (last 50)")
c = None
try:
    from polymarket_us import PolymarketUS
    c = PolymarketUS(key_id=config.KEY_ID, secret_key=config.SECRET_KEY)
    resp = c.portfolio.activities({"types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"], "limit": 50})
    activities = resp.get("activities") or []
except Exception as e:
    print(f"  exchange unavailable ({e}) — DB-only audit")
    activities = []

# The account also carries the OLD band-filter bot's history: resolutions
# from before THIS bot's first live entry are expected to have no DB row
# and are not flagged (verified 2026-07-13: all such flags were July 1-2
# positions from the prior bot).
cutoff = q("SELECT MIN(created_at) FROM positions WHERE live=1")
cutoff = (cutoff[0][0] if cutoff and cutoff[0][0] else "9999")

# Markets the operator traded by hand live in manual_trades, NOT positions —
# a resolution there is tracked, just in the other ledger. Without this the
# audit false-flags every resolving manual bet as "untracked" (fra-esp,
# 2026-07-15). Any live manual row for the slug counts as tracked; if it's
# still open the sync just hasn't closed it yet (informational, not a flag).
manual_rows = {}
try:
    for slug_m, status_m in q(
            "SELECT market_slug, status FROM manual_trades WHERE live=1") or []:
        manual_rows.setdefault(slug_m, set()).add(status_m)
except Exception:
    pass  # older DB without manual_trades — nothing manual to reconcile

mismatched = []
skipped_precutoff = 0
skipped_manual = 0
for a in activities:
    pr = a.get("positionResolution") or {}
    slug = pr.get("marketSlug") or "?"
    b = pr.get("beforePosition") or {}
    try:
        ex_pnl = round(float(b["cashValue"]["value"]) - float(b["cost"]["value"]), 2)
    except Exception:
        ex_pnl = None
    row = q("SELECT status, pnl FROM positions WHERE market_slug=?", slug)
    if not row:
        if slug in manual_rows:
            # Tracked as a manual trade. Only note it if it's still open —
            # that means the manual sync hasn't settled it yet, worth an eye.
            if manual_rows[slug] & {"open", "pending"}:
                print(f"  (manual trade {slug} resolved on exchange but its row is "
                      f"still open — the next manual sync should close it)")
            else:
                skipped_manual += 1
            continue
        upd = str(pr.get("updateTime") or a.get("updateTime") or "").replace("Z", "+00:00")
        if upd and upd < cutoff:
            skipped_precutoff += 1
            continue  # pre-dates this bot's live history — the other bot's position
        flag(f"{slug}: resolved on exchange, NO DB ROW (untracked position!)")
        continue
    status, db_pnl = row[0]
    if status not in ("won", "lost", "push"):
        flag(f"{slug}: resolved on exchange but DB status={status} (missed settlement!)")
    elif ex_pnl is not None and db_pnl is not None and abs(ex_pnl - db_pnl) > 0.02:
        flag(f"{slug}: pnl drift DB={db_pnl} vs exchange={ex_pnl}")
        mismatched.append(slug)
if skipped_precutoff:
    print(f"  ({skipped_precutoff} pre-cutover resolutions skipped — other bot's history)")
if skipped_manual:
    print(f"  ({skipped_manual} resolutions skipped — tracked in manual_trades, already closed)")
if activities and not FLAGS:
    print("  all resolutions matched — good")

# ------------------------------------- C. multi-activity investigation
if mismatched:
    section("C. ALL resolution activities for drifted markets (multi-resolution check)")
    for slug in mismatched[:5]:
        try:
            r = c.portfolio.activities({"marketSlug": slug,
                                        "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"],
                                        "limit": 10})
            acts = r.get("activities") or []
        except Exception as e:
            print(f"  {slug}: fetch failed ({e})")
            continue
        print(f"  {slug}: {len(acts)} resolution activit{'y' if len(acts)==1 else 'ies'}")
        for i, a in enumerate(acts):
            pr = a.get("positionResolution") or {}
            b = pr.get("beforePosition") or {}
            try:
                p = round(float(b["cashValue"]["value"]) - float(b["cost"]["value"]), 2)
            except Exception:
                p = "?"
            print(f"    #{i}: pnl={p} update={pr.get('updateTime','?')[:19]} side={pr.get('side','?')}")

# ------------------------ D. Manual closed rows vs exchange realized P&L
# The 2026-07-15 sign bug booked underdog LOSSES as gains and still passed
# this audit, because B only reconciles SETTLED *positions* rows against the
# exchange — it never checked manual cashed_out rows. Close that: compare each
# exchange-derived manual row's stored P&L to the exchange's OWN realized
# figure (Σ effectiveRealizedPnl on closing fills, plus any resolution on a
# held remainder). A flipped sign shows up as a ~2x drift here.
section("D. Manual trades vs exchange realized P&L")
if c is None:
    print("  exchange unavailable — skipped")
else:
    try:
        import manual_sync
    except Exception as e:
        manual_sync = None
        print(f"  manual_sync import failed ({e}) — skipped")
    if manual_sync is not None:
        closed = q("""SELECT market_slug, status, pnl FROM manual_trades
                      WHERE live=1 AND status IN ('cashed_out','won','lost','push')""")
        checked = 0
        for slug, status, db_pnl in closed:
            fills = manual_sync.manual_fills(c, slug)
            if not fills:
                continue  # hand-entered row with no exchange trace — can't reconcile
            _net, _oc, realized = manual_sync.summarize(fills)
            res = manual_sync.resolution_pnl(c, slug)
            if res is manual_sync.RESOLUTION_CHECK_FAILED:
                continue  # transient — don't flag on an unverifiable check
            expected = round(realized + res[0], 2) if isinstance(res, tuple) else realized
            if db_pnl is None:
                flag(f"manual {slug}: {status} but pnl NULL (exchange realized {expected:+.2f})")
            elif abs(db_pnl - expected) > 0.02:
                flag(f"manual {slug}: DB pnl {db_pnl:+.2f} vs exchange realized "
                     f"{expected:+.2f} (drift {db_pnl - expected:+.2f}) — direction/sign bug?")
            else:
                checked += 1
        if checked:
            print(f"  {checked} manual row(s) match the exchange's realized P&L — good")
        else:
            print("  no exchange-derived manual rows to reconcile")

print(f"\n{'='*50}\n{FLAGS} issue(s) flagged" if FLAGS else f"\n{'='*50}\nCLEAN — reporting inputs are consistent")
sys.exit(1 if FLAGS else 0)
