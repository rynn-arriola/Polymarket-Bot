"""Reporting: period stats, per-sport and per-divergence-bucket breakdowns,
CLI summary, and Discord webhooks (status embeds, per-settlement messages,
once-daily digest).

Ported from the proven reporting in the original bot.py (same period
attribution rule, same embed layout, same ANSI coloring, same field-size
chunking), minus the pregame/live split that bot doesn't apply here, plus
the analytics this strategy actually lives or dies by:

- per-position model_prob / market_price / divergence in every settlement
  message, so each result can be judged against what the model believed;
- a divergence-bucket table (does a bigger model-vs-market gap actually
  win more?) — this is THE question a divergence strategy has to answer,
  and it's in the daily digest and `status` output from day one.

Period attribution: a bet belongs to the period it was PLACED in, and its
win/loss/P&L counts there whenever it eventually settles — Entries and
Record for a period always describe the same cohort of bets.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from db import db, period_bounds_utc, today

log = logging.getLogger("divergence_bot.reporting")

BOT_NAME = "Polybot"


def pct(won: int, lost: int) -> str:
    settled = won + lost
    if not settled:
        return "n/a"
    return f"{100.0 * won / settled:.1f}%"


def money(value: float) -> str:
    if value > 0:
        return f"+${value:.2f}"
    if value < 0:
        return f"-${abs(value):.2f}"
    return "$0.00"


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

def stats_for_period(kind: str) -> dict:
    if kind in ("today", "week", "month"):
        start, end = period_bounds_utc(kind)
        created_filter = "created_at >= ? AND created_at < ?"
        args = (start, end)
    elif kind == "overall":
        created_filter = "1=1"
        args = ()
    else:
        raise ValueError(f"Unknown stats period: {kind}")
    # Scope every stat to the current mode so live (real money) and dry-run
    # (simulated) positions never blend in one report — the DB holds both.
    created_filter += " AND live = ?"
    args = args + (1 if config.LIVE else 0,)

    def count(extra: str) -> int:
        return db(f"SELECT COUNT(*) FROM positions WHERE {created_filter} {extra}", args, fetch=True)[0][0]

    entries = count("")
    open_positions = count("AND status IN ('pending','open')")
    won = count("AND status='won'")
    lost = count("AND status='lost'")
    push = count("AND status='push'")
    # P&L/staked are explicitly scoped to SETTLED statuses, not just
    # pnl IS NOT NULL — a hand-repaired or anomalous row (e.g. a cancelled
    # row carrying a pnl) must never leak into the totals silently.
    pnl = db(
        f"""SELECT COALESCE(SUM(pnl),0) FROM positions WHERE {created_filter}
            AND pnl IS NOT NULL AND status IN ('won','lost','push')""",
        args, fetch=True,
    )[0][0]
    avg_div = db(
        f"SELECT AVG(divergence) FROM positions WHERE {created_filter} AND divergence IS NOT NULL",
        args, fetch=True,
    )[0][0]
    # Closing-line value: did the market move toward our bets by tip-off?
    # avg_clv = mean(closing - entry) for our side; beat = share that improved.
    avg_clv, clv_beat, clv_n = db(
        f"""SELECT AVG(closing_price - market_price),
                   AVG(CASE WHEN closing_price > market_price THEN 1.0 ELSE 0.0 END),
                   COUNT(*)
            FROM positions
            WHERE {created_filter} AND closing_price IS NOT NULL AND market_price IS NOT NULL""",
        args, fetch=True,
    )[0]
    # Yield = realized P&L as a share of money actually staked (settled bets) —
    # the return-on-turnover health metric, independent of bankroll size.
    staked = db(
        f"""SELECT COALESCE(SUM(stake),0) FROM positions WHERE {created_filter}
            AND pnl IS NOT NULL AND status IN ('won','lost','push')""",
        args, fetch=True,
    )[0][0]
    return {
        "entries": entries,
        "open": open_positions,
        "won": won,
        "lost": lost,
        "push": push,
        "settled": won + lost + push,
        "pnl": pnl or 0.0,
        "win_rate": pct(won, lost),
        "avg_divergence": avg_div,
        "avg_clv": avg_clv,
        "clv_beat": clv_beat,
        "clv_n": clv_n or 0,
        "staked": staked or 0.0,
        "yield": (pnl / staked) if staked else None,
    }


def stats_by_sport() -> list[dict]:
    """All-time record per sport, worst win rate first so underperformers
    are immediately visible; sports with no settled result yet sort last."""
    rows = db(
        """SELECT sport,
                   COUNT(*) AS entries,
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS won,
                   SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS lost,
                   SUM(CASE WHEN status='push' THEN 1 ELSE 0 END) AS push,
                   COALESCE(SUM(CASE WHEN status IN ('won','lost','push')
                                     THEN pnl ELSE 0 END), 0) AS pnl
            FROM positions
            WHERE live = ?
            GROUP BY sport""",
        (1 if config.LIVE else 0,),
        fetch=True,
    )
    results = []
    for sport, entries, won, lost, push, pnl in rows:
        results.append({
            "sport": sport, "entries": entries, "won": won, "lost": lost,
            "push": push, "settled": won + lost + push,
            "pnl": pnl or 0.0, "win_rate": pct(won, lost),
        })

    def sort_key(r):
        settled = r["won"] + r["lost"]
        if settled == 0:
            return (1, 0.0, -r["entries"])
        return (0, r["won"] / settled, -r["entries"])

    results.sort(key=sort_key)
    return results


DIVERGENCE_BUCKETS = [(0.05, 0.10), (0.10, 0.15), (0.15, 0.21)]


def stats_by_divergence() -> list[dict]:
    """Settled results grouped by how big the model-vs-market gap was at
    entry. THE core validation for this strategy: if bigger divergence
    doesn't win more (relative to the price paid), the edge isn't real.
    Also compares the model's average predicted win prob per bucket against
    the actual win rate — live calibration, on money-where-mouth-is games."""
    live = 1 if config.LIVE else 0
    out = []
    for lo, hi in DIVERGENCE_BUCKETS:
        row = db(
            """SELECT COUNT(*),
                       SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                       COALESCE(SUM(pnl), 0),
                       AVG(model_prob)
                FROM positions
                WHERE divergence >= ? AND divergence < ?
                  AND status IN ('won','lost','push') AND live = ?""",
            (lo, hi, live), fetch=True,
        )[0]
        settled, won, lost, pnl, avg_model = row
        # CLV over ALL positions in this bucket with a captured close (open OR
        # settled) — the leading edge signal: does the market move toward our
        # bigger-divergence bets? Available at game start, before settlement.
        avg_clv, clv_n = db(
            """SELECT AVG(closing_price - market_price), COUNT(*)
               FROM positions
               WHERE divergence >= ? AND divergence < ?
                 AND closing_price IS NOT NULL AND market_price IS NOT NULL AND live = ?""",
            (lo, hi, live), fetch=True,
        )[0]
        out.append({
            "bucket": f"{lo:.0%}-{hi:.0%}",
            "settled": settled or 0,
            "won": won or 0,
            "lost": lost or 0,
            "pnl": pnl or 0.0,
            "win_rate": pct(won or 0, lost or 0),
            "avg_model_prob": avg_model,
            "avg_clv": avg_clv,
            "clv_n": clv_n or 0,
        })
    return out


# Entry-price bands: is each segment beating its own break-even? A position
# bought at price p breaks even at a win rate of p (plus ~1.2% fees) — so a
# "low win rate" band is fine as long as its actual WR exceeds the average
# price paid there. Bands are coarse on purpose: settled counts per band are
# small for a long time, and narrow bands would just show noise.
PRICE_BUCKETS = [(0.05, 0.15), (0.15, 0.30), (0.30, 0.50),
                 (0.50, 0.70), (0.70, 0.85), (0.85, 0.95)]
FEE_PCT = 0.012  # worst-case taker fee, same figure the thresholds assume


def stats_by_price() -> list[dict]:
    """Settled results grouped by ENTRY PRICE. Answers 'are the longshot
    (low win rate) trades actually earning?': a band is healthy when its
    actual win rate beats avg entry price + fees (its break-even), unhealthy
    when it doesn't — regardless of how low the raw win rate looks."""
    live = 1 if config.LIVE else 0
    out = []
    for lo, hi in PRICE_BUCKETS:
        settled, won, lost, pnl, avg_price = db(
            """SELECT COUNT(*),
                       SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                       COALESCE(SUM(pnl), 0),
                       AVG(price)
                FROM positions
                WHERE price >= ? AND price < ?
                  AND status IN ('won','lost','push') AND live = ?""",
            (lo, hi, live), fetch=True,
        )[0]
        won, lost = won or 0, lost or 0
        breakeven = (avg_price + FEE_PCT) if avg_price is not None else None
        wr = won / (won + lost) if (won + lost) else None
        out.append({
            "bucket": f"{lo:.0%}-{hi:.0%}",
            "settled": settled or 0, "won": won, "lost": lost,
            "pnl": pnl or 0.0,
            "win_rate": wr,                    # fraction or None
            "breakeven": breakeven,            # avg price + fees, or None
            "beats": (None if wr is None or breakeven is None
                      else wr > breakeven),
        })
    return out


def manual_stats_for_period(kind: str) -> dict:
    """Stats for trades recorded by manual_trades.py.

    Kept separate from bot `positions` stats on purpose: manual bets do not
    have model_prob/market_price/CLV semantics and must not dilute edge
    validation. Period attribution follows the bot convention: a manual trade
    belongs to the period it was recorded/opened in.
    """
    if kind in ("today", "week", "month"):
        start, end = period_bounds_utc(kind)
        created_filter = "created_at >= ? AND created_at < ?"
        args = (start, end)
    elif kind == "overall":
        created_filter = "1=1"
        args = ()
    else:
        raise ValueError(f"Unknown manual stats period: {kind}")
    created_filter += " AND live = ?"
    args = args + (1 if config.LIVE else 0,)

    try:
        row = db(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN status IN ('pending','open') THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='push' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='cashed_out' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),
                       COALESCE(SUM(CASE WHEN status IN ('won','lost','push','cashed_out')
                                         THEN pnl ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN status != 'cancelled' THEN stake ELSE 0 END), 0)
                FROM manual_trades WHERE {created_filter}""",
            args,
            fetch=True,
        )[0]
    except Exception:
        # Older DB before db_init() creates manual_trades, or a deployment
        # where the CLI has not been used yet. Reporting should stay quiet.
        row = (0, 0, 0, 0, 0, 0, 0, 0.0, 0.0)

    entries, open_n, won, lost, push, cashed, cancelled, pnl, staked = row
    won, lost = won or 0, lost or 0
    return {
        "entries": entries or 0,
        "open": open_n or 0,
        "won": won,
        "lost": lost,
        "push": push or 0,
        "cashed_out": cashed or 0,
        "cancelled": cancelled or 0,
        "settled": won + lost + (push or 0) + (cashed or 0),
        "pnl": pnl or 0.0,
        "staked": staked or 0.0,
        "win_rate": pct(won, lost),
        "yield": ((pnl or 0.0) / staked) if staked else None,
    }


def manual_stats_by_sport() -> list[dict]:
    live = 1 if config.LIVE else 0
    try:
        rows = db(
            """SELECT sport, COUNT(*),
                      COALESCE(SUM(CASE WHEN status IN ('won','lost','push','cashed_out')
                                        THEN pnl ELSE 0 END), 0)
               FROM manual_trades
               WHERE live = ?
               GROUP BY sport
               ORDER BY COUNT(*) DESC""",
            (live,),
            fetch=True,
        )
    except Exception:
        return []
    return [{"sport": sport or "UNKNOWN", "entries": n or 0, "pnl": pnl or 0.0}
            for sport, n, pnl in rows]


def signal_stats_for_period(kind: str) -> dict:
    """Paper-only results for every valid candidate the bot observed.

    These numbers never feed real P&L, bankroll, risk, or settlement reports.
    They exist to show whether price/risk filters are declining good signals.
    """
    if kind in ("today", "week", "month"):
        start, end = period_bounds_utc(kind)
        created_filter = "created_at >= ? AND created_at < ?"
        args = (start, end)
    elif kind == "overall":
        created_filter = "1=1"
        args = ()
    else:
        raise ValueError(f"Unknown signal stats period: {kind}")
    created_filter += " AND live = ?"
    args = args + (1 if config.LIVE else 0,)
    try:
        row = db(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN decision='traded' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN decision!='traded' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='push' THEN 1 ELSE 0 END),
                       COALESCE(SUM(CASE WHEN status IN ('won','lost','push')
                                         THEN paper_pnl ELSE 0 END), 0),
                       AVG(closing_price - market_price),
                       AVG(CASE
                             WHEN closing_price IS NULL OR market_price IS NULL THEN NULL
                             WHEN closing_price > market_price THEN 1.0
                             ELSE 0.0
                           END),
                       SUM(CASE WHEN closing_price IS NOT NULL AND market_price IS NOT NULL
                                THEN 1 ELSE 0 END)
                FROM shadow_signals WHERE {created_filter}""",
            args,
            fetch=True,
        )[0]
    except Exception:
        row = (0, 0, 0, 0, 0, 0, 0, 0.0, None, None, 0)
    entries, open_n, traded, skipped, won, lost, push, pnl, avg_clv, clv_beat, clv_n = row
    return {
        "entries": entries or 0,
        "open": open_n or 0,
        "traded": traded or 0,
        "not_traded": skipped or 0,
        "won": won or 0,
        "lost": lost or 0,
        "push": push or 0,
        "settled": (won or 0) + (lost or 0) + (push or 0),
        "paper_pnl": pnl or 0.0,
        "win_rate": pct(won or 0, lost or 0),
        "avg_clv": avg_clv,
        "clv_beat": clv_beat,
        "clv_n": clv_n or 0,
    }


def signal_not_traded_reasons(kind: str = "overall", limit: int = 4) -> list[tuple[str, int]]:
    """Most common final reasons valid signals did not become orders."""
    if kind in ("today", "week", "month"):
        start, end = period_bounds_utc(kind)
        created_filter = "created_at >= ? AND created_at < ?"
        args = (start, end)
    elif kind == "overall":
        created_filter = "1=1"
        args = ()
    else:
        raise ValueError(f"Unknown signal reasons period: {kind}")
    args = args + (1 if config.LIVE else 0, limit)
    try:
        rows = db(
            f"""SELECT COALESCE(decision_reason, decision), COUNT(*)
                FROM shadow_signals
                WHERE {created_filter} AND live=? AND decision != 'traded'
                GROUP BY COALESCE(decision_reason, decision)
                ORDER BY COUNT(*) DESC, COALESCE(decision_reason, decision)
                LIMIT ?""",
            args,
            fetch=True,
        )
    except Exception:
        return []
    return [(str(reason or "unknown"), count or 0) for reason, count in rows]


def summary_snapshot() -> dict:
    return {
        "today": stats_for_period("today"),
        "week": stats_for_period("week"),
        "month": stats_for_period("month"),
        "overall": stats_for_period("overall"),
        "manual_today": manual_stats_for_period("today"),
        "manual_week": manual_stats_for_period("week"),
        "manual_month": manual_stats_for_period("month"),
        "manual_overall": manual_stats_for_period("overall"),
        "signals_today": signal_stats_for_period("today"),
        "signals_week": signal_stats_for_period("week"),
        "signals_month": signal_stats_for_period("month"),
        "signals_overall": signal_stats_for_period("overall"),
        "mode": "LIVE" if config.LIVE else "DRY-RUN",
        "updated": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------
# CLI formatting (the `status` command)
# ------------------------------------------------------------------

def format_period(label: str, stats: dict) -> str:
    avg_div = stats.get("avg_divergence")
    div_line = f"\n  Avg divergence at entry: {avg_div:+.1%}" if avg_div is not None else ""
    clv_line = ""
    if stats.get("clv_n"):
        clv_line = (f"\n  CLV: {stats['avg_clv']:+.1%} avg | beat close "
                    f"{stats['clv_beat']:.0%} (n={stats['clv_n']})")
    y = stats.get("yield")
    yield_str = f" (yield {y:+.1%} on ${stats['staked']:.0f})" if y is not None else ""
    return (
        f"{label}\n"
        f"  P&L: {stats['pnl']:+.2f}{yield_str}\n"
        f"  Record: {stats['won']}W / {stats['lost']}L / {stats['push']}P\n"
        f"  Win rate: {stats['win_rate']}\n"
        f"  Entries: {stats['entries']} | Open: {stats['open']}{div_line}{clv_line}"
    )


def format_manual_period(label: str, stats: dict) -> str:
    if not stats.get("entries"):
        return ""
    y = stats.get("yield")
    yield_str = f" (yield {y:+.1%} on ${stats['staked']:.0f})" if y is not None else ""
    return (
        f"{label}\n"
        f"  P&L: {stats['pnl']:+.2f}{yield_str}\n"
        f"  Record: {stats['won']}W / {stats['lost']}L / {stats['push']}P / "
        f"{stats['cashed_out']} cashout / {stats['cancelled']} cancel\n"
        f"  Win rate: {stats['win_rate']}\n"
        f"  Entries: {stats['entries']} | Open: {stats['open']}"
    )


def format_signal_period(label: str, stats: dict) -> str:
    if not stats.get("entries"):
        return ""
    clv = ""
    if stats.get("clv_n"):
        clv = f"\n  CLV: {stats['avg_clv']:+.1%} avg | beat close {stats['clv_beat']:.0%} (n={stats['clv_n']})"
    return (
        f"{label} (paper only - excluded from real P&L)\n"
        f"  Estimated P&L: {stats['paper_pnl']:+.2f}\n"
        f"  Record: {stats['won']}W / {stats['lost']}L / {stats['push']}P\n"
        f"  Win rate: {stats['win_rate']}\n"
        f"  Signals: {stats['entries']} | Traded: {stats['traded']} | Not traded: {stats['not_traded']} | Open: {stats['open']}{clv}"
    )


def format_summary_text(snapshot: dict, title: str = "POLYBOT SUMMARY") -> str:
    text = (
        f"\n=== {title} ===\n"
        f"Mode: {snapshot['mode']}\n\n"
        f"{format_period('Today', snapshot['today'])}\n\n"
        f"{format_period('This Week', snapshot['week'])}\n\n"
        f"{format_period('This Month', snapshot['month'])}\n\n"
        f"{format_period('Overall', snapshot['overall'])}\n"
    )
    manual = format_manual_period("Manual Trades", snapshot.get("manual_overall", {}))
    if manual:
        text += f"\n{manual}\n"
    signals = format_signal_period("Valid Signals", snapshot.get("signals_overall", {}))
    if signals:
        text += f"\n{signals}\n"
    return text


FRESHNESS_FILE = "elo_freshness.json"


def _load_freshness() -> dict:
    try:
        with open(FRESHNESS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _age_hours(last_built: str | None):
    if not last_built:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last_built)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def freshness_oneline() -> str:
    """Compact per-sport rating age, for the startup log."""
    fresh = _load_freshness()
    if not fresh:
        return "no freshness data yet (rebuild ratings to generate)"
    parts = []
    for sport in sorted(fresh):
        age = _age_hours(fresh[sport].get("last_built"))
        parts.append(f"{sport} {age:.0f}h" if age is not None else f"{sport} n/a")
    return " | ".join(parts)


def format_freshness(stale_hours: float | None = None) -> str:
    """Per-sport ratings freshness table for `status`: how long ago each sport
    rebuilt, its latest ingested game date, and a STALE flag past the guard."""
    fresh = _load_freshness()
    if not fresh:
        return "\nRatings freshness: no elo_freshness.json yet — run build_ratings.py.\n"
    if stale_hours is None:
        stale_hours = getattr(config, "RATINGS_STALE_HOURS", 24)
    lines = ["\nRatings freshness (does the collected data actually reach the model?):",
             f"  {'sport':>8} {'rebuilt':>10} {'latest game':>13} {'games':>8}"]
    for sport in sorted(fresh):
        m = fresh[sport]
        age = _age_hours(m.get("last_built"))
        age_str = f"{age:.0f}h ago" if age is not None else "n/a"
        flag = "  <-- STALE" if (stale_hours and age is not None and age > stale_hours) else ""
        lines.append(f"  {sport:>8} {age_str:>10} {str(m.get('latest_game_date') or 'n/a'):>13} "
                     f"{m.get('n_games', 0):>8}{flag}")
    return "\n".join(lines) + "\n"


def format_divergence_table() -> str:
    rows = stats_by_divergence()
    if not any(r["settled"] or r["clv_n"] for r in rows):
        return "\nEdge validation by divergence size: no data yet.\n"
    lines = ["\nEdge validation by divergence size (record/model/P&L = settled; CLV = leading, all captured):",
             f"  {'bucket':>8} {'n':>5} {'record':>10} {'win%':>6} "
             f"{'model':>6} {'CLV':>8} {'P&L':>9}"]
    for r in rows:
        model = f"{r['avg_model_prob']:.0%}" if r["avg_model_prob"] is not None else "n/a"
        clv = f"{r['avg_clv']:+.1%}" if r.get("avg_clv") is not None else "n/a"
        lines.append(
            f"  {r['bucket']:>8} {r['settled']:>5} "
            f"{str(r['won']) + 'W-' + str(r['lost']) + 'L':>10} "
            f"{r['win_rate']:>6} {model:>6} {clv:>8} {r['pnl']:>+9.2f}"
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# Discord helpers (same conventions as bot.py: ANSI inside ```ansi fences,
# emoji fallback for mobile where ANSI doesn't render)
# ------------------------------------------------------------------

def embed_color(pnl: float) -> int:
    if pnl > 0:
        return 0x2ECC71
    if pnl < 0:
        return 0xE74C3C
    return 0x95A5A6


def pnl_emoji(pnl: float) -> str:
    if pnl > 0:
        return "🟢"
    if pnl < 0:
        return "🔴"
    return "⚪"


def pnl_ansi(pnl: float) -> str:
    code = "32" if pnl > 0 else "31" if pnl < 0 else "2;37"
    return f"\x1b[{code}m{pnl:+.2f}\x1b[0m"


def settlement_ansi(text: str, pnl: float) -> str:
    code = "32" if pnl > 0 else "31" if pnl < 0 else "2;37"
    clean = str(text or "Unknown").replace("`", "'")
    return f"\x1b[{code}m{clean}\x1b[0m"


def _post(webhook: str, payload: dict, what: str) -> bool:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            # Discord's Cloudflare edge blocks the default urllib UA
            # (Python-urllib/x.y) as a bot — Discord error code 1010.
            "User-Agent": "DivergenceBotDiscordWebhook/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning(f"Discord {what} post failed: {e}")
        return False


# ------------------------------------------------------------------
# Ops alerts: forward every WARNING+ log record to a dedicated Discord
# webhook — the "needs developer attention" channel (API failures, stale
# models, untracked orders, settlement problems...). Hooked into logging
# itself so every existing and future log.warning/error/critical in the
# codebase is covered without instrumenting call sites.
# ------------------------------------------------------------------

_LEVEL_EMOJI = {"WARNING": "🟡", "ERROR": "🔴", "CRITICAL": "🚨"}


class DiscordErrorHandler(logging.Handler):
    """Batches and dedupes WARNING+ records, then posts one embed.

    Spam control: identical messages within a batch collapse to one line
    with a ×count; WARNINGs post at most every batch_seconds; ERROR and
    CRITICAL flush fast (min urgent_gap seconds between posts) because an
    untracked live order shouldn't wait ten minutes. A failed post keeps
    the buffer for the next attempt. NEVER raises into the logging call,
    and never forwards reporting's own logger (a failed webhook post logs
    a warning — forwarding it would recurse forever).
    """

    MAX_PENDING = 40          # unique messages held per batch; extras are counted
    MAX_LINE = 200            # chars of each message shown

    def __init__(self, webhook: str, batch_minutes: float = 10,
                 urgent_gap_seconds: float = 60):
        super().__init__(level=logging.WARNING)
        self.webhook = webhook
        self.batch_seconds = batch_minutes * 60
        self.urgent_gap = urgent_gap_seconds
        self.pending: dict[str, dict] = {}   # key -> {level, levelno, msg, first, count}
        self.dropped = 0
        self.last_post = 0.0                 # monotonic; 0 = first problem posts immediately

    def emit(self, record: logging.LogRecord):
        try:
            if record.name.startswith("divergence_bot.reporting"):
                return
            msg = record.getMessage()
            key = f"{record.levelname}|{msg[:300]}"
            entry = self.pending.get(key)
            if entry:
                entry["count"] += 1
            elif len(self.pending) < self.MAX_PENDING:
                from db import REPORT_TZ
                self.pending[key] = {
                    "level": record.levelname, "levelno": record.levelno,
                    "msg": msg, "count": 1,
                    "first": datetime.now(REPORT_TZ).strftime("%H:%M"),
                }
            else:
                self.dropped += 1
            import time
            now = time.monotonic()
            gap = self.urgent_gap if record.levelno >= logging.ERROR else self.batch_seconds
            if now - self.last_post >= gap:
                self._flush(now)
        except Exception:
            pass  # a broken alert channel must never break the bot

    def _flush(self, now: float):
        if not self.pending:
            return
        self.last_post = now  # even on failure — never hammer Discord
        entries = sorted(self.pending.values(), key=lambda e: -e["levelno"])
        lines = []
        for e in entries:
            count = f" ×{e['count']}" if e["count"] > 1 else ""
            text = e["msg"][: self.MAX_LINE] + ("…" if len(e["msg"]) > self.MAX_LINE else "")
            lines.append(f"{_LEVEL_EMOJI.get(e['level'], '⚪')} `{e['first']}`{count} {text}")
        desc = "\n".join(lines)[:3800]
        if self.dropped:
            desc += f"\n…plus {self.dropped} more (see divergence_bot.errors.log)"
        worst = entries[0]["levelno"]
        payload = {
            "username": BOT_NAME,
            "embeds": [{
                "title": "🚨 Needs attention" if worst >= logging.CRITICAL
                         else "🔴 Errors" if worst >= logging.ERROR else "🟡 Warnings",
                "description": desc,
                "color": 0xE74C3C if worst >= logging.ERROR else 0xF1C40F,
                "footer": {"text": "full history: python divergence_bot.py errors"},
            }],
        }
        if _post(self.webhook, payload, "error-alert"):
            self.pending.clear()
            self.dropped = 0


def attach_discord_error_handler(logger: logging.Logger) -> "DiscordErrorHandler | None":
    """Attach the ops-alert handler (no-op when the webhook isn't configured)."""
    webhook = getattr(config, "DISCORD_ERRORS_WEBHOOK_URL", "").strip()
    if not webhook:
        return None
    handler = DiscordErrorHandler(
        webhook, batch_minutes=getattr(config, "ERROR_ALERT_BATCH_MINUTES", 10))
    logger.addHandler(handler)
    return handler


# ------------------------------------------------------------------
# Daily ops digest: read the day's error log, group problems into known
# categories, and post ONE "what might need fixing" summary to the errors
# webhook. The live alerts (above) say something happened; this turns a
# whole day of them into a ranked to-do list.
# ------------------------------------------------------------------

ERRORS_LOG_FILE = "divergence_bot.errors.log"  # keep in sync with divergence_bot.ERROR_LOG

# (pattern, category title, what-to-fix hint) — first match wins. Extend as
# new recurring problems show up in the channel.
_OPS_CATEGORIES = (
    (r"UNTRACKED", "Untracked orders/positions",
     "Reconcile positions.db vs the exchange NOW — real money may be untracked"),
    (r"DB insert failed|database is locked", "Position DB writes failing",
     "Check positions.db health/locks on the server"),
    (r"no Elo match for", "Team/player name mismatches",
     "Add the names to name_match.ALIASES (harvest with suggest_aliases.py)"),
    (r"STALE ratings|ratings stale", "Ratings not rebuilding",
     "Refresh pipeline is failing — check refresh_data.log and data sources"),
    (r"Data refresh exited|reload FAILED|Could not start data refresh", "Data refresh pipeline",
     "Rebuild subprocess is failing — check refresh_data.log"),
    (r"player model .*stale|Drive blocked|UNAVAILABLE this run", "LoL player data stale",
     "Oracle's Elixir fetch blocked — usually Drive quota; retries automatically"),
    (r"Could not fetch account balance|Balance API|Balance fetch failed", "Balance API failing",
     "Check API keys and Polymarket status — sizing falls back to cached/estimated balance"),
    (r"settlement (checks keep failing|endpoint)|Unrecognized settlement", "Settlement problems",
     "Verify the settlement endpoint/auth; positions may sit open past their game"),
    (r"Cancel failed|order rejected|orders? create|POSSIBLE UNTRACKED", "Order placement/cancel errors",
     "Check the orders API and recent order handling in the log"),
    (r"Could not fetch portfolio|Could not sweep exchange", "Portfolio API failing",
     "Fill confirmation degraded — check API auth/status"),
    (r"roster fetch failed|fetch failed", "Data-source fetch failures (fail-open)",
     "Transient source outage (vlr/OpenDota/Leaguepedia/ESPN...) — guards skip, trading continues; "
     "investigate only if ONE source fails persistently for days"),
    (r"Unparseable start time", "Market schema drift",
     "Polymarket changed a field format — update the parser"),
    (r"Discord .* post failed", "Discord webhooks failing",
     "Check webhook URLs / Discord status (reports only, trading unaffected)"),
    (r"Pagination cap|market list may be incomplete|rejected pagination", "Market list pagination",
     "Market fetching degraded — check SDK/API compatibility"),
)


def _read_recent_errors(hours: float = 24) -> list[tuple[str, str]]:
    """[(levelname, message)] for errors-log lines from the last `hours`.
    A ROLLING window, not calendar-day: a digest posted at 6pm must still
    cover last evening's problems (peak game time), which a same-date filter
    would silently drop forever. Log timestamps are server-local (logging
    default), so compare against local now."""
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    try:
        with open(ERRORS_LOG_FILE, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (WARNING|ERROR|CRITICAL) (.*)", line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append((m.group(2), m.group(3).strip()))
    except FileNotFoundError:
        pass
    return out


def _generic_key(msg: str) -> str:
    """Fallback grouping for messages no category matches: collapse the
    variable parts (numbers, quoted names, slugs) so repeats group."""
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", msg)
    s = re.sub(r"\b[\w]+(-[\w]+){2,}\b", "<slug>", s)   # market slugs
    s = re.sub(r"\d+(\.\d+)?", "N", s)
    return s[:120]


def post_discord_ops_digest() -> bool:
    """One message: today's problems grouped into categories, ranked by
    severity then volume, each with a what-to-fix hint. Posts a green
    all-clear when the day had no problems (so silence != broken webhook)."""
    webhook = getattr(config, "DISCORD_ERRORS_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    errors = _read_recent_errors(24)
    day = datetime.now().strftime("%b %d")
    if not errors:
        return _post(webhook, {"username": BOT_NAME, "embeds": [{
            "title": f"✅ Ops digest — {day}",
            "description": "No warnings or errors logged in the last 24h. Nothing needs fixing.",
            "color": 0x2ECC71,
        }]}, "ops-digest")

    rank = {"CRITICAL": 2, "ERROR": 1, "WARNING": 0}
    groups: dict[str, dict] = {}
    for level, msg in errors:
        for pat, title, hint in _OPS_CATEGORIES:
            if re.search(pat, msg, re.I):
                key, cat_title, cat_hint = title, title, hint
                break
        else:
            key = f"other:{_generic_key(msg)}"
            cat_title, cat_hint = None, None
        g = groups.setdefault(key, {"title": cat_title, "hint": cat_hint,
                                    "count": 0, "worst": 0, "example": msg})
        g["count"] += 1
        if rank[level] > g["worst"]:
            g["worst"], g["example"] = rank[level], msg

    ordered = sorted(groups.values(), key=lambda g: (-g["worst"], -g["count"]))
    lines = []
    for g in ordered[:12]:
        emoji = ("🚨", "🔴", "🟡")[2 - g["worst"]]
        head = g["title"] or "Uncategorized"
        lines.append(f"{emoji} **{head}** ×{g['count']}")
        if g["hint"]:
            lines.append(f"   fix: {g['hint']}")
        # Keep the raw log line generous — these digests get copy-pasted
        # into a triage chat, and the specifics (slug, name, exception)
        # are what make the paste actionable.
        lines.append(f"   e.g. `{g['example'][:220]}`")
    if len(ordered) > 12:
        lines.append(f"…plus {len(ordered) - 12} more group(s) — see the errors log")

    worst = max(g["worst"] for g in ordered)
    return _post(webhook, {"username": BOT_NAME, "embeds": [{
        "title": f"🛠️ Ops digest — {day} (last 24h): {len(errors)} problem(s), {len(ordered)} distinct",
        "description": "\n".join(lines)[:3900],
        "color": 0xE74C3C if worst >= 1 else 0xF1C40F,
        "footer": {"text": "full history: python divergence_bot.py errors"},
    }]}, "ops-digest")


_LAST_OPS_DIGEST_DATE: str | None = None


def maybe_post_ops_digest():
    """Post the ops digest once per day after OPS_DIGEST_HOUR (default 22, in
    the reporting timezone) — same cadence pattern as the daily P&L digest."""
    global _LAST_OPS_DIGEST_DATE
    from db import REPORT_TZ
    now = datetime.now(REPORT_TZ)
    if now.hour >= getattr(config, "OPS_DIGEST_HOUR", 22) and today() != _LAST_OPS_DIGEST_DATE:
        post_discord_ops_digest()
        _LAST_OPS_DIGEST_DATE = today()


def discord_field(label: str, stats: dict, inline: bool) -> dict:
    # CLV intentionally NOT shown here — it lives on its own webhook
    # (post_discord_clv), kept off the status/summary channel.
    return {
        "name": f"{pnl_emoji(stats['pnl'])} {label}",
        "value": (
            "```ansi\n"
            f"P&L:      {pnl_ansi(stats['pnl'])}\n"
            f"Record:   {stats['won']}W / {stats['lost']}L / {stats['push']}P\n"
            f"Win rate: {stats['win_rate']}\n"
            f"Entries:  {stats['entries']} (open {stats['open']})\n"
            "```"
        ),
        "inline": inline,
    }


def discord_manual_field(stats: dict, inline: bool = False) -> dict | None:
    if not stats.get("entries"):
        return None
    y = stats.get("yield")
    yield_str = f"  Yield:    {y:+.1%}\n" if y is not None else ""
    return {
        "name": f"{pnl_emoji(stats['pnl'])} Manual Trades",
        "value": (
            "```ansi\n"
            f"P&L:      {pnl_ansi(stats['pnl'])}\n"
            f"Record:   {stats['won']}W / {stats['lost']}L / {stats['push']}P\n"
            f"Cashout:  {stats['cashed_out']} | Cancelled: {stats['cancelled']}\n"
            f"Entries:  {stats['entries']} (open {stats['open']})\n"
            f"{yield_str}"
            "```"
        ),
        "inline": inline,
    }


def discord_signal_field(stats: dict, inline: bool = False) -> dict | None:
    if not stats.get("entries"):
        return None
    clv_line = ""
    if stats.get("clv_n"):
        clv_line = f"CLV:      {stats['avg_clv']:+.1%} (beat {stats['clv_beat']:.0%}, n={stats['clv_n']})\n"
    blocks = signal_not_traded_reasons()
    blocks_line = ""
    if blocks:
        shown = "; ".join(f"{count}x {reason[:52]}" for reason, count in blocks)
        blocks_line = f"Blocked:  {shown}\n"
    return {
        "name": "Paper Signals (not real P&L)",
        "value": (
            "```ansi\n"
            f"Est P&L:  {stats['paper_pnl']:+.2f}\n"
            f"Record:    {stats['won']}W / {stats['lost']}L / {stats['push']}P\n"
            f"Signals:   {stats['entries']} (traded {stats['traded']}, not traded {stats['not_traded']})\n"
            f"Open:      {stats['open']}\n"
            f"{clv_line}"
            f"{blocks_line}"
            "```"
        ),
        "inline": inline,
    }


def post_discord_summary(reason: str = "Status update") -> bool:
    webhook = getattr(config, "DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    snapshot = summary_snapshot()
    mode_badge = "🔴 LIVE" if snapshot["mode"] == "LIVE" else "🧪 DRY-RUN"
    fields = [
        discord_field("Today", snapshot["today"], True),
        discord_field("This Week", snapshot["week"], True),
        discord_field("This Month", snapshot["month"], True),
        discord_field("Overall", snapshot["overall"], False),
    ]
    manual_field = discord_manual_field(snapshot.get("manual_overall", {}), False)
    if manual_field:
        fields.append(manual_field)
    signal_field = discord_signal_field(snapshot.get("signals_overall", {}), False)
    if signal_field:
        fields.append(signal_field)
    payload = {
        "username": BOT_NAME,
        "embeds": [
            {
                "title": "📊 Polybot Status",
                "description": reason,
                "color": embed_color(snapshot["today"]["pnl"]),
                "fields": fields,
                "footer": {"text": f"{BOT_NAME} | {mode_badge}"},
                "timestamp": snapshot["updated"],
            }
        ],
    }
    return _post(webhook, payload, "status")


def _chunk_ansi_lines(lines: list[str]) -> list[list[str]]:
    """Discord caps an embed field's value at 1024 chars — split lines into
    as many ```ansi fenced fields as needed (bot.py hit this live with 17
    sports; carrying the fix forward)."""
    FIELD_LIMIT = 1024
    FENCE_OVERHEAD = len("```ansi\n") + len("\n```")
    chunks, current, current_len = [], [], FENCE_OVERHEAD
    for line in lines:
        added = len(line) + 1
        if current and current_len + added > FIELD_LIMIT:
            chunks.append(current)
            current, current_len = [], FENCE_OVERHEAD
        current.append(line)
        current_len += added
    if current:
        chunks.append(current)
    return chunks


def post_discord_daily_digest() -> bool:
    """Per-sport record + divergence-bucket edge validation, one message a
    day — decision-support numbers, not live monitoring."""
    webhook = getattr(config, "DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    sport_rows = stats_by_sport()
    if not sport_rows:
        return False

    # Lead with the all-time headline: P&L, yield, win rate, and CLV.
    ov = stats_for_period("overall")
    ov_code = "32" if ov["pnl"] > 0 else "31" if ov["pnl"] < 0 else "2;37"
    ov_yield = f"  yield {ov['yield']:+.1%}" if ov.get("yield") is not None else ""
    fields = [{
        "name": "📊 Overall (all-time)",
        "value": ("```ansi\n"
                  f"\x1b[{ov_code}mP&L     {ov['pnl']:+.2f}{ov_yield}\x1b[0m\n"
                  f"Record  {ov['won']}W-{ov['lost']}L-{ov['push']}P   win rate {ov['win_rate']}\n"
                  f"Entries {ov['entries']} (open {ov['open']})\n```"),
        "inline": False,
    }]
    signals_overall = signal_stats_for_period("overall")
    if signals_overall.get("entries"):
        signal_lines = [f"{'period':<10}{'sig':>5}{'taken':>7}{'skip':>6}{'record':>10}{'est P&L':>10}{'CLV':>8}"]
        for label, kind in (("Today", "today"), ("Overall", "overall")):
            s = signal_stats_for_period(kind)
            record = f"{s['won']}W-{s['lost']}L"
            clv = f"{s['avg_clv']:+.2%}" if s.get("clv_n") else "--"
            code = "32" if s["paper_pnl"] > 0 else "31" if s["paper_pnl"] < 0 else "2;37"
            signal_lines.append(
                f"\x1b[{code}m{label:<10}{s['entries']:>5}{s['traded']:>7}{s['not_traded']:>6}"
                f"{record:>10}{s['paper_pnl']:>+10.2f}{clv:>8}\x1b[0m"
            )
        fields.append({
            "name": "Paper signals (all valid candidates; excluded from real P&L)",
            "value": "```ansi\n" + "\n".join(signal_lines) + "\n```",
            "inline": False,
        })

    manual = manual_stats_for_period("overall")
    if manual.get("entries"):
        my = f"  yield {manual['yield']:+.1%}" if manual.get("yield") is not None else ""
        m_code = "32" if manual["pnl"] > 0 else "31" if manual["pnl"] < 0 else "2;37"
        fields.append({
            "name": "Manual Trades (separate ledger)",
            "value": ("```ansi\n"
                      f"\x1b[{m_code}mP&L     {manual['pnl']:+.2f}{my}\x1b[0m\n"
                      f"Record  {manual['won']}W-{manual['lost']}L-{manual['push']}P   "
                      f"cashout {manual['cashed_out']}   cancelled {manual['cancelled']}\n"
                      f"Entries {manual['entries']} (open {manual['open']})\n```"),
            "inline": False,
        })
    # Per-sport win rate is NOT here — it's on the CLV tracker webhook now.

    div_rows = stats_by_divergence()
    if any(r["settled"] for r in div_rows):
        div_lines = [f"{'BUCKET':<9}{'RECORD':<9}{'WR':>7}{'MODEL':>7}{'P&L':>10}"]
        for r in div_rows:
            model = f"{r['avg_model_prob']:.0%}" if r["avg_model_prob"] is not None else "n/a"
            rec = f"{r['won']}W-{r['lost']}L"
            code = "32" if r["pnl"] > 0 else "31" if r["pnl"] < 0 else "2;37"
            div_lines.append(
                f"\x1b[{code}m{r['bucket']:<9}{rec:<9}{r['win_rate']:>7}{model:>7}{r['pnl']:>+10.2f}\x1b[0m"
            )
        fields.append({
            "name": "Edge validation — by divergence size at entry",
            "value": "```ansi\n" + "\n".join(div_lines) + "\n```",
            "inline": False,
        })

    # Freshness / staleness — so a silently-failed refresh pings you here.
    fresh = _load_freshness()
    if fresh:
        stale_h = getattr(config, "RATINGS_STALE_HOURS", 24)
        fl = [f"{'SPORT':<10}{'AGE':>6}{'LATEST GAME':>14}"]
        for sport in sorted(fresh):
            age = _age_hours(fresh[sport].get("last_built"))
            is_stale = bool(stale_h) and age is not None and age > stale_h
            code = "31" if is_stale else "32"
            age_str = f"{age:.0f}h" if age is not None else "n/a"
            latest = fresh[sport].get("latest_game_date") or "n/a"
            fl.append(f"\x1b[{code}m{sport:<10}{age_str:>6}{latest:>14}"
                      f"{'  STALE-SKIPPED' if is_stale else ''}\x1b[0m")
        fields.append({
            "name": "Ratings freshness (stale sports are auto-skipped)",
            "value": "```ansi\n" + "\n".join(fl) + "\n```",
            "inline": False,
        })

    payload = {
        "username": BOT_NAME,
        "embeds": [
            {
                "title": "📈 Polybot Daily Digest (all-time)",
                "description": "Per-sport record (worst win rate first) and whether "
                               "bigger model-vs-market gaps are actually winning.",
                "color": 0x5865F2,
                "fields": fields,
                "footer": {"text": f"{BOT_NAME} | daily digest"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return _post(webhook, payload, "daily digest")


def post_discord_clv(reason: str = "CLV update") -> bool:
    """Dedicated closing-line-value report on its own webhook: CLV per period
    plus CLV by divergence bucket (the leading edge signal — does the market
    move toward our bets by tip-off?). Scoped to the current mode like every
    other stat. Blank webhook = no-op."""
    webhook = getattr(config, "DISCORD_CLV_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    # Overall CLV drives the whole embed's color + headline verdict, so the
    # green/red read is instant even on mobile (where ANSI in code blocks does
    # not render — the emoji and sidebar color do).
    overall = stats_for_period("overall")
    overall_clv = overall.get("avg_clv") if overall.get("clv_n") else None

    period_lines = [f"{'period':<10}{'avg CLV':>9}{'beat':>7}{'n':>6}"]
    for label, kind in (("Today", "today"), ("This Week", "week"), ("Overall", "overall")):
        s = stats_for_period(kind)
        if s.get("clv_n"):
            code = "32" if s["avg_clv"] > 0 else "31" if s["avg_clv"] < 0 else "2;37"
            # Leading emoji renders everywhere; it sits OUTSIDE the fixed-width
            # columns so it can't throw off the monospace alignment.
            period_lines.append(f"\x1b[{code}m{pnl_emoji(s['avg_clv'])} {label:<8}{s['avg_clv']:>+9.2%}"
                                f"{s['clv_beat']:>7.0%}{s['clv_n']:>6}\x1b[0m")
        else:
            period_lines.append(f"⚪ {label:<8}{'--':>9}{'--':>7}{0:>6}")
    fields = [{
        "name": "CLV by period",
        "value": "```ansi\n" + "\n".join(period_lines) + "\n```",
        "inline": False,
    }]

    div_rows = stats_by_divergence()
    if any(r.get("clv_n") for r in div_rows):
        bl = [f"{'bucket':<9}{'avg CLV':>9}{'n':>6}"]
        for r in div_rows:
            if r.get("avg_clv") is not None:
                code = "32" if r["avg_clv"] > 0 else "31" if r["avg_clv"] < 0 else "2;37"
                bl.append(f"\x1b[{code}m{pnl_emoji(r['avg_clv'])} {r['bucket']:<7}{r['avg_clv']:>+9.2%}{r['clv_n']:>6}\x1b[0m")
            else:
                bl.append(f"⚪ {r['bucket']:<7}{'--':>9}{r['clv_n']:>6}")
        fields.append({
            "name": "CLV by divergence size (bigger gap should beat the close more)",
            "value": "```ansi\n" + "\n".join(bl) + "\n```",
            "inline": False,
        })

    # Win rate vs break-even by ENTRY PRICE band — "is the low-win-rate
    # segment actually earning?" A band is green when actual WR beats its
    # avg price + fees, red when it doesn't. n stays small for a while;
    # bands without settled results show as gray placeholders.
    price_rows = stats_by_price()
    if any(r["settled"] for r in price_rows):
        pl = [f"{'price':<9}{'record':<9}{'WR':>6}{'brkev':>7}{'P&L':>9}"]
        for r in price_rows:
            if r["win_rate"] is None:
                if r["settled"]:  # pushes only
                    continue
                pl.append(f"⚪ {r['bucket']:<7}{'--':<9}{'--':>6}{'--':>7}{'--':>9}")
                continue
            code = "32" if r["beats"] else "31"
            mark = pnl_emoji(1 if r["beats"] else -1)
            rec = f"{r['won']}W-{r['lost']}L"
            pl.append(f"\x1b[{code}m{mark} {r['bucket']:<7}{rec:<9}{r['win_rate']:>6.0%}"
                      f"{r['breakeven']:>7.0%}{r['pnl']:>+9.2f}\x1b[0m")
        fields.append({
            "name": "Win rate vs break-even, by entry price (WR must beat brkev = avg price + fees)",
            "value": "```ansi\n" + "\n".join(pl) + "\n```",
            "inline": False,
        })

    paper_overall = signal_stats_for_period("overall")
    if paper_overall.get("entries"):
        paper_lines = []
        for label, kind in (("Today", "today"), ("Overall", "overall")):
            s = signal_stats_for_period(kind)
            clv = (f"{s['avg_clv']:+.2%} (n={s['clv_n']})"
                   if s.get("clv_n") else "--")
            paper_lines.append(
                f"{label}: {s['entries']} signals | traded {s['traded']} | "
                f"not traded {s['not_traded']} | {s['won']}W-{s['lost']}L | "
                f"est P&L {s['paper_pnl']:+.2f} | CLV {clv}"
            )
        fields.append({
            "name": "Paper signals (all valid candidates; excluded from real P&L)",
            "value": "\n".join(paper_lines),
            "inline": False,
        })

    # Per-sport win rate — lives here on the CLV tracker (kept off the summary).
    sport_rows = stats_by_sport()
    if sport_rows:
        sl = [f"{'SPORT':<10}{'RECORD':<12}{'WR':>7}{'P&L':>10}{'ENT':>6}"]
        for r in sport_rows:
            wr = r["win_rate"] if (r["won"] + r["lost"]) else "n/a"
            rec = f"{r['won']}W-{r['lost']}L-{r['push']}P"
            code = "32" if r["pnl"] > 0 else "31" if r["pnl"] < 0 else "2;37"
            sl.append(f"\x1b[{code}m{r['sport']:<10}{rec:<12}{wr:>7}{r['pnl']:>+10.2f}{r['entries']:>6}\x1b[0m")
        for i, chunk in enumerate(_chunk_ansi_lines(sl)):
            fields.append({
                "name": "Per-sport win rate (worst first)" if i == 0 else "​",
                "value": "```ansi\n" + "\n".join(chunk) + "\n```",
                "inline": False,
            })

    manual = manual_stats_for_period("overall")
    if manual.get("entries"):
        my = f"  yield {manual['yield']:+.1%}" if manual.get("yield") is not None else ""
        m_code = "32" if manual["pnl"] > 0 else "31" if manual["pnl"] < 0 else "2;37"
        fields.append({
            "name": "Manual trades (not part of CLV/model edge)",
            "value": ("```ansi\n"
                      f"\x1b[{m_code}mP&L     {manual['pnl']:+.2f}{my}\x1b[0m\n"
                      f"Record  {manual['won']}W-{manual['lost']}L-{manual['push']}P   "
                      f"cashout {manual['cashed_out']}   cancelled {manual['cancelled']}\n"
                      f"Entries {manual['entries']} (open {manual['open']})\n```"),
            "inline": False,
        })

    mode_badge = "🔴 LIVE" if config.LIVE else "🧪 DRY-RUN"
    # Headline verdict from overall CLV: green embed + 🟢 = the market is
    # moving toward our bets (edge looks real); red + 🔴 = moving against us;
    # neutral until there's data. Falls back to the brand teal when no CLV
    # has been captured yet (nothing to color).
    if overall_clv is None:
        sidebar, verdict = 0x1ABC9C, "⚪ No CLV captured yet"
    else:
        sidebar = embed_color(overall_clv)
        verdict = (f"{pnl_emoji(overall_clv)} Overall CLV {overall_clv:+.2%} — "
                   + ("market moving TOWARD our bets (edge looks real)" if overall_clv > 0
                      else "market moving AGAINST our bets" if overall_clv < 0
                      else "flat"))
    payload = {
        "username": BOT_NAME,
        "embeds": [{
            "title": f"{pnl_emoji(overall_clv) if overall_clv is not None else '📉'} Polybot CLV Report",
            "description": f"**{verdict}**\n{reason} — CLV = how far the market moved toward our "
                           "bets by tip-off. Consistently positive = real edge.",
            "color": sidebar,
            "fields": fields,
            "footer": {"text": f"{BOT_NAME} | {mode_badge}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    return _post(webhook, payload, "CLV report")


def post_discord_settlements(position_ids: list[int]) -> int:
    webhook = getattr(config, "DISCORD_SETTLEMENT_WEBHOOK_URL", "").strip()
    if not webhook or not position_ids:
        return 0

    posted = 0
    for pid in position_ids:
        rows = db(
            """SELECT market_slug, matchup, side, status, pnl, live, settled_at, sport,
                       model_prob, market_price, divergence, price
               FROM positions WHERE id=?""",
            (pid,), fetch=True,
        )
        if not rows:
            continue
        (slug, matchup, side, status, pnl, live, settled_at, sport,
         model_prob, market_price, divergence, price) = rows[0]
        pnl = float(pnl or 0)
        outcome = str(status or "settled").upper()
        match = matchup or slug
        mode_badge = "LIVE" if live else "DRY-RUN"
        color_name = "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "EVEN"
        match_label = f"Match ({sport})" if sport and sport != "UNKNOWN" else "Match"
        # The strategy-specific block: the price we actually filled at, and
        # what the model believed vs what Polymarket implied — so every
        # settlement can be judged as a forecast AND against the price we got,
        # not just as a win or a loss.
        try:
            entry_line = f"Filled:  ${float(price):.2f}"
        except (TypeError, ValueError):
            entry_line = "Filled:  n/a"
        if model_prob is not None and market_price is not None:
            model_line = (f"Model:   {model_prob:.0%}  vs Polymarket {market_price:.0%}  "
                          f"(edge {divergence:+.1%})")
        else:
            model_line = "Model:   n/a"
        at_entry_text = f"{entry_line}\n{model_line}"
        payload = {
            "username": BOT_NAME,
            "embeds": [
                {
                    "author": {"name": "Polybot Settlement"},
                    "description": f"# {pnl_emoji(pnl)} {outcome} | {color_name}",
                    "color": embed_color(pnl),
                    "fields": [
                        {
                            "name": match_label,
                            "value": f"```ansi\n{settlement_ansi(match, pnl)}\n```",
                            "inline": False,
                        },
                        {
                            "name": "Pick",
                            "value": f"```ansi\n{settlement_ansi(side or 'Unknown', pnl)}\n```",
                            "inline": True,
                        },
                        {
                            "name": "Result",
                            "value": f"```ansi\n{settlement_ansi(outcome, pnl)}\n```",
                            "inline": True,
                        },
                        {
                            "name": "Profit / Loss",
                            "value": f"```ansi\n{settlement_ansi(money(pnl), pnl)}\n```",
                            "inline": True,
                        },
                        {
                            "name": "At entry",
                            "value": f"```ansi\n{settlement_ansi(at_entry_text, pnl)}\n```",
                            "inline": False,
                        },
                    ],
                    "footer": {"text": f"{BOT_NAME} | {mode_badge}"},
                    "timestamp": settled_at or datetime.now(timezone.utc).isoformat(),
                }
            ],
        }
        if _post(webhook, payload, f"settlement ({slug})"):
            posted += 1
    return posted


def post_discord_manual_trade(trade_id: int) -> bool:
    """Settlement-webhook card for a manually tracked trade.

    Uses the same webhook/style as bot settlements, but the wording is clear
    that this was not a bot-entered model trade and therefore has no model vs
    market edge block.
    """
    webhook = getattr(config, "DISCORD_SETTLEMENT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    rows = db(
        """SELECT market_slug, matchup, side, status, pnl, live, closed_at,
                  sport, price, quantity, stake, close_price, close_reason, notes
           FROM manual_trades WHERE id=?""",
        (trade_id,),
        fetch=True,
    )
    if not rows:
        return False
    (slug, matchup, side, status, pnl, live, closed_at, sport, price, quantity,
     stake, close_price, close_reason, notes) = rows[0]
    pnl = float(pnl or 0)
    outcome = str(status or "updated").upper()
    mode_badge = "LIVE" if live else "DRY-RUN"
    color_name = "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "EVEN"
    match = matchup or slug or f"Manual trade #{trade_id}"
    match_label = f"Manual Trade ({sport})" if sport else "Manual Trade"
    try:
        entry_line = f"Filled:  ${float(price):.2f} x {float(quantity):g} = ${float(stake):.2f}"
    except (TypeError, ValueError):
        entry_line = "Filled:  n/a"
    close_line = ""
    if close_price is not None:
        try:
            close_line = f"\nClose:   ${float(close_price):.2f}"
        except (TypeError, ValueError):
            pass
    reason_line = f"\nReason:  {close_reason}" if close_reason else ""
    notes_line = f"\nNotes:   {notes}" if notes else ""
    payload = {
        "username": BOT_NAME,
        "embeds": [
            {
                "author": {"name": "Polybot Manual Trade"},
                "description": f"# {pnl_emoji(pnl)} {outcome} | {color_name}",
                "color": embed_color(pnl),
                "fields": [
                    {
                        "name": match_label,
                        "value": f"```ansi\n{settlement_ansi(match, pnl)}\n```",
                        "inline": False,
                    },
                    {
                        "name": "Pick",
                        "value": f"```ansi\n{settlement_ansi(side or 'Unknown', pnl)}\n```",
                        "inline": True,
                    },
                    {
                        "name": "Result",
                        "value": f"```ansi\n{settlement_ansi(outcome, pnl)}\n```",
                        "inline": True,
                    },
                    {
                        "name": "Profit / Loss",
                        "value": f"```ansi\n{settlement_ansi(money(pnl), pnl)}\n```",
                        "inline": True,
                    },
                    {
                        "name": "Manual entry",
                        "value": f"```ansi\n{settlement_ansi(entry_line + close_line + reason_line + notes_line, pnl)}\n```",
                        "inline": False,
                    },
                ],
                "footer": {"text": f"{BOT_NAME} | manual | {mode_badge}"},
                "timestamp": closed_at or datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return _post(webhook, payload, f"manual trade ({trade_id})")


# ------------------------------------------------------------------
# Daily digest scheduling (once per day at/after 22:00 reporting time)
# ------------------------------------------------------------------

_LAST_DIGEST_DATE = None


def maybe_post_daily_digest():
    global _LAST_DIGEST_DATE
    from db import REPORT_TZ
    now = datetime.now(REPORT_TZ)
    if now.hour >= 22 and today() != _LAST_DIGEST_DATE:
        post_discord_daily_digest()
        _LAST_DIGEST_DATE = today()
