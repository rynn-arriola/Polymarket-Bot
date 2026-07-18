"""Edge report: WHERE is the strategy actually profitable?

The companion to audit_reporting.py — that one verifies the numbers are
TRUE, this one shows what they SAY. Read-only; run on the server and paste
the whole output back for analysis:

    python3 edge_report.py

Every slice carries its sample size, and slices too thin to trust are
labeled — the biggest mistake this report guards against is pruning or
sizing up a segment on 5 bets of noise. Reading order: CLV first (the
leading signal — it converges weeks before W/L does), then win rate vs
each slice's own break-even (avg entry price + fees), then P&L.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import config

MIN_N = 15        # below this many settled bets, a slice's W/L says ~nothing
MIN_CLV_N = 10    # CLV is meaningful sooner, but not at 3 samples
FEE_PCT = 0.012   # worst-case taker fee (same figure the thresholds assume)

LIVE = 1 if config.LIVE else 0
con = sqlite3.connect("file:positions.db?mode=ro", uri=True)


def q(sql, *a):
    return con.execute(sql, a).fetchall()


def one(sql, *a):
    return q(sql, *a)[0]


def fmt_pct(x, none="    --"):
    return f"{x:+7.1%}" if x is not None else none


def fmt_wr(x, none="    --"):
    return f"{x:7.1%}" if x is not None else none


def thin(n, need=MIN_N):
    return "" if (n or 0) >= need else f"  (low n — not decision-grade)"


SETTLED = "status IN ('won','lost','push')"
# CLV counts FILLED trades only. A cancelled row's captured close belongs to
# an order that never filled — usually BECAUSE the price ran away from it, so
# including it systematically flatters CLV with money never at risk.
CLV_FILLED = "status IN ('open','won','lost','push')"

print("=" * 74)
print(f"EDGE REPORT | {'LIVE' if LIVE else 'DRY-RUN'} | generated {datetime.now(timezone.utc).isoformat()[:16]}Z")
first = one(f"SELECT MIN(created_at) FROM positions WHERE live={LIVE}")[0]
print(f"history since {str(first)[:10]} | paste this ENTIRE output back for analysis")
print("=" * 74)

# ---------------------------------------------------------------- overall
n, w, l, p, pnl, staked = one(f"""
    SELECT COUNT(*), SUM(status='won'), SUM(status='lost'), SUM(status='push'),
           COALESCE(SUM(pnl),0), COALESCE(SUM(stake),0)
    FROM positions WHERE live={LIVE} AND {SETTLED}""")
clv, beat, clv_n = one(f"""
    SELECT AVG(closing_price - market_price),
           AVG(closing_price > market_price), COUNT(*)
    FROM positions WHERE live={LIVE} AND {CLV_FILLED}
    AND closing_price IS NOT NULL AND market_price IS NOT NULL""")
open_n = one(f"SELECT COUNT(*) FROM positions WHERE live={LIVE} AND status IN ('open','pending')")[0]

print(f"\nOVERALL: {n} settled ({w}W-{l}L-{p}P, {open_n} open)")
print(f"  P&L {pnl:+.2f} on ${staked:.0f} staked  ->  yield {pnl/staked:+.1%}" if staked else "  no settled stakes yet")
print(f"  CLV {fmt_pct(clv)} avg | beat close {beat:.0%} of {clv_n}{thin(clv_n, MIN_CLV_N)}"
      if clv_n else "  CLV: none captured yet")

# Capture coverage: filled trades past every capture chance (pre-start window
# + post-start fallback) that still miss a close are absent from EVERY CLV
# figure in this report — say so up front rather than let the subset pass as
# the whole.
_grace = getattr(config, "CLOSING_FALLBACK_MINUTES", 60) + 10
_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_grace)).isoformat()
cap, elig = one(f"""
    SELECT COALESCE(SUM(closing_price IS NOT NULL),0), COUNT(*)
    FROM positions WHERE live={LIVE} AND {CLV_FILLED}
    AND game_start IS NOT NULL AND datetime(game_start) <= datetime(?)""", _cutoff)
if elig and cap < elig:
    print(f"  CLV capture coverage: {cap}/{elig} — {elig - cap} filled trade(s) MISSING a close; "
          f"every CLV figure below is computed on the captured subset only")
elif elig:
    print(f"  CLV capture coverage: {cap}/{elig} — complete")

# --------------------------------------------------------------- by sport
print(f"\nBY SPORT{'':<24}(sorted by settled n; CLV is the early signal)")
print(f"  {'sport':<9}{'n':>4}{'record':>10}{'WR':>8}{'P&L':>9}{'yield':>9}{'avgCLV':>8}{'clvN':>5}")
rows = q(f"""
    SELECT sport, COUNT(*), SUM(status='won'), SUM(status='lost'),
           COALESCE(SUM(pnl),0), COALESCE(SUM(stake),0)
    FROM positions WHERE live={LIVE} AND {SETTLED} GROUP BY sport ORDER BY COUNT(*) DESC""")
for sport, sn, sw, sl, spnl, sstk in rows:
    sclv, sclv_n = one(f"""SELECT AVG(closing_price - market_price), COUNT(*) FROM positions
                           WHERE live={LIVE} AND {CLV_FILLED} AND sport=?
                           AND closing_price IS NOT NULL
                           AND market_price IS NOT NULL""", sport)
    wr = sw / (sw + sl) if (sw + sl) else None
    yld = spnl / sstk if sstk else None
    print(f"  {sport:<9}{sn:>4}{f'{sw}W-{sl}L':>10}{fmt_wr(wr):>8}{spnl:>+9.2f}"
          f"{fmt_pct(yld):>9}{fmt_pct(sclv):>8}{sclv_n or 0:>5}{thin(sn)}")

# ---------------------------------------------------- by divergence bucket
print(f"\nBY DIVERGENCE AT ENTRY   (bigger model-vs-market gap should do better)")
print(f"  {'bucket':<9}{'n':>4}{'record':>10}{'WR':>8}{'model':>7}{'P&L':>9}{'avgCLV':>8}")
for lo, hi in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.21)):
    bn, bw, bl, bpnl, bmodel = one(f"""
        SELECT COUNT(*), SUM(status='won'), SUM(status='lost'),
               COALESCE(SUM(pnl),0), AVG(model_prob)
        FROM positions WHERE live={LIVE} AND {SETTLED} AND divergence >= ? AND divergence < ?""",
        lo, hi)
    bclv = one(f"""SELECT AVG(closing_price - market_price) FROM positions
                   WHERE live={LIVE} AND {CLV_FILLED} AND divergence >= ? AND divergence < ?
                   AND closing_price IS NOT NULL AND market_price IS NOT NULL""", lo, hi)[0]
    wr = bw / (bw + bl) if (bw or 0) + (bl or 0) else None
    model = f"{bmodel:.0%}" if bmodel is not None else "--"
    print(f"  {f'{lo:.0%}-{hi:.0%}':<9}{bn:>4}{f'{bw or 0}W-{bl or 0}L':>10}{fmt_wr(wr):>8}"
          f"{model:>7}{bpnl:>+9.2f}{fmt_pct(bclv):>8}{thin(bn)}")

# -------------------------------------------------------- by price band
print(f"\nBY ENTRY PRICE           (WR must beat brkev = avg price + {FEE_PCT:.1%} fees)")
print(f"  {'band':<9}{'n':>4}{'record':>10}{'WR':>8}{'brkev':>7}{'P&L':>9}{'verdict':>9}")
for lo, hi in ((0.05, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.95)):
    pn, pw, pl_, ppnl, pavg = one(f"""
        SELECT COUNT(*), SUM(status='won'), SUM(status='lost'),
               COALESCE(SUM(pnl),0), AVG(price)
        FROM positions WHERE live={LIVE} AND {SETTLED} AND price >= ? AND price < ?""", lo, hi)
    if not pn:
        continue
    wr = pw / (pw + pl_) if (pw or 0) + (pl_ or 0) else None
    brk = (pavg + FEE_PCT) if pavg is not None else None
    verdict = ("--" if wr is None or brk is None
               else "EARNING" if wr > brk else "BLEEDING")
    print(f"  {f'{lo:.0%}-{hi:.0%}':<9}{pn:>4}{f'{pw or 0}W-{pl_ or 0}L':>10}{fmt_wr(wr):>8}"
          f"{brk:>7.0%}{ppnl:>+9.2f}{verdict:>9}{thin(pn)}")

# ------------------------------------------------------------- by side
print(f"\nBY SIDE                  (long = favorite/Yes, short = inverse side)")
print(f"  {'side':<9}{'n':>4}{'record':>10}{'WR':>8}{'avg$':>7}{'P&L':>9}{'avgCLV':>8}")
for label, cond in (("long", "is_long=1 OR is_long IS NULL"), ("short", "is_long=0")):
    sn, sw, sl, spnl, sprice = one(f"""
        SELECT COUNT(*), SUM(status='won'), SUM(status='lost'),
               COALESCE(SUM(pnl),0), AVG(price)
        FROM positions WHERE live={LIVE} AND {SETTLED} AND ({cond})""")
    if not sn:
        continue
    sclv = one(f"""SELECT AVG(closing_price - market_price) FROM positions
                   WHERE live={LIVE} AND {CLV_FILLED} AND ({cond})
                   AND closing_price IS NOT NULL
                   AND market_price IS NOT NULL""")[0]
    wr = sw / (sw + sl) if (sw or 0) + (sl or 0) else None
    avgp = f"{sprice:.2f}" if sprice is not None else "--"
    print(f"  {label:<9}{sn:>4}{f'{sw or 0}W-{sl or 0}L':>10}{fmt_wr(wr):>8}"
          f"{avgp:>7}{spnl:>+9.2f}{fmt_pct(sclv):>8}{thin(sn)}")

# ------------------------------------------------------ last 7 settle-days
print(f"\nLAST 7 SETTLE-DAYS       (momentum check, settle-date attribution)")
for day, dn, dpnl in q(f"""
        SELECT substr(settled_at,1,10), COUNT(*), COALESCE(SUM(pnl),0)
        FROM positions WHERE live={LIVE} AND {SETTLED} AND settled_at IS NOT NULL
        GROUP BY substr(settled_at,1,10) ORDER BY 1 DESC LIMIT 7"""):
    print(f"  {day}  {dn:>3} settled  {dpnl:>+9.2f}")

# ------------------------------------------------------- recommendations
# Conservative, sample-size-gated candidates only — this section refuses to
# conclude anything from noise. Final calls belong to the human analysis of
# the full report, one config change at a time.
recs = []

if clv_n and clv_n >= MIN_CLV_N:
    if (clv or 0) > 0.005:
        recs.append(f"Overall CLV {clv:+.1%} over {clv_n}: the edge signal is POSITIVE — "
                    f"keep current settings, let sample grow before sizing up.")
    elif (clv or 0) < -0.005:
        recs.append(f"Overall CLV {clv:+.1%} over {clv_n}: market moves AGAINST entries — "
                    f"do not loosen anything; consider tightening thresholds before adding sports.")

for sport, sn, sw, sl, spnl, sstk in rows:
    sclv, sclv_n = one(f"""SELECT AVG(closing_price - market_price), COUNT(*) FROM positions
                           WHERE live={LIVE} AND {CLV_FILLED} AND sport=?
                           AND closing_price IS NOT NULL
                           AND market_price IS NOT NULL""", sport)
    if (sclv_n or 0) >= MIN_CLV_N and (sclv or 0) <= -0.01:
        recs.append(f"{sport}: CLV {sclv:+.1%} over {sclv_n} — PAUSE CANDIDATE "
                    f"(market consistently beats our entries there).")
    elif (sclv_n or 0) >= MIN_CLV_N and (sclv or 0) >= 0.01 and sn >= MIN_N and spnl > 0:
        recs.append(f"{sport}: CLV {sclv:+.1%} (n={sclv_n}) AND P&L {spnl:+.2f} (n={sn}) — "
                    f"strongest slice; a modest MAX_PER_SPORT_PER_DAY bump is defensible.")

for lo, hi in ((0.05, 0.15), (0.85, 0.95)):
    pn, pw, pl_, pavg = one(f"""
        SELECT COUNT(*), SUM(status='won'), SUM(status='lost'), AVG(price)
        FROM positions WHERE live={LIVE} AND {SETTLED} AND price >= ? AND price < ?""", lo, hi)
    if pn and pn >= MIN_N:
        wr = pw / (pw + pl_) if (pw or 0) + (pl_ or 0) else None
        if wr is not None and pavg is not None and wr < pavg + FEE_PCT:
            knob = "raising PRICE_FLOOR" if lo < 0.5 else "lowering PRICE_CEIL"
            recs.append(f"price band {lo:.0%}-{hi:.0%}: WR {wr:.0%} under break-even "
                        f"{pavg + FEE_PCT:.0%} over {pn} — consider {knob}.")

for label, cond in (("long", "is_long=1 OR is_long IS NULL"), ("short", "is_long=0")):
    sn, spnl = one(f"SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM positions "
                   f"WHERE live={LIVE} AND {SETTLED} AND ({cond})")
    sclv, sclv_n = one(f"""SELECT AVG(closing_price - market_price), COUNT(*) FROM positions
                           WHERE live={LIVE} AND {CLV_FILLED} AND ({cond})
                           AND closing_price IS NOT NULL
                           AND market_price IS NOT NULL""")
    if sn >= MIN_N and spnl < 0 and (sclv_n or 0) >= MIN_CLV_N and (sclv or 0) < -0.01:
        recs.append(f"{label} side: P&L {spnl:+.2f} (n={sn}) AND CLV {sclv:+.1%} — "
                    f"review; if it persists another {MIN_N} bets, "
                    f"{'set LONG_ONLY=True' if label == 'short' else 'investigate long entries'}.")

print(f"\nRECOMMENDATIONS (auto-generated, sample-size-gated)")
if recs:
    for r in recs:
        print(f"  • {r}")
else:
    rate = n / max(1, (datetime.now(timezone.utc)
                       - datetime.fromisoformat(str(first))).days or 1) if first and n else 0
    print(f"  • No decision-grade signal yet — every slice is below its sample gate.")
    if rate:
        print(f"    At ~{rate:.0f} settled/day, first per-sport verdicts in roughly "
              f"{max(1, int(MIN_CLV_N / max(rate/4, 0.1)))} more days. Keep collecting.")

print(f"""
{'=' * 74}
READING GUIDE (for whoever analyzes this):
  1. CLV first — positive avg CLV = market moves toward our bets = real edge
     signal, meaningful from ~{MIN_CLV_N} samples. W/L needs ~{MIN_N}+ per slice.
  2. Low-WR bands are fine IF WR > their break-even (avg price + fees).
  3. Actions live in config.py: DIVERGENCE_THRESHOLDS per sport, PRICE_FLOOR/
     CEIL, LONG_ONLY, MAX_PER_SPORT_PER_DAY. Change on evidence, ONE at a time,
     then watch the next week's report before the next change.
{'=' * 74}""")
