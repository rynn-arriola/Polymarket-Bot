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
import urllib.error
import urllib.request
from datetime import datetime, timezone

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
    pnl = db(
        f"SELECT COALESCE(SUM(pnl),0) FROM positions WHERE {created_filter} AND pnl IS NOT NULL",
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
        f"SELECT COALESCE(SUM(stake),0) FROM positions WHERE {created_filter} AND pnl IS NOT NULL",
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
                   COALESCE(SUM(pnl), 0) AS pnl
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


def summary_snapshot() -> dict:
    return {
        "today": stats_for_period("today"),
        "week": stats_for_period("week"),
        "month": stats_for_period("month"),
        "overall": stats_for_period("overall"),
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


def format_summary_text(snapshot: dict, title: str = "POLYBOT SUMMARY") -> str:
    return (
        f"\n=== {title} ===\n"
        f"Mode: {snapshot['mode']}\n\n"
        f"{format_period('Today', snapshot['today'])}\n\n"
        f"{format_period('This Week', snapshot['week'])}\n\n"
        f"{format_period('This Month', snapshot['month'])}\n\n"
        f"{format_period('Overall', snapshot['overall'])}\n"
    )


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


def post_discord_summary(reason: str = "Status update") -> bool:
    webhook = getattr(config, "DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    snapshot = summary_snapshot()
    mode_badge = "🔴 LIVE" if snapshot["mode"] == "LIVE" else "🧪 DRY-RUN"
    payload = {
        "username": BOT_NAME,
        "embeds": [
            {
                "title": "📊 Polybot Status",
                "description": reason,
                "color": embed_color(snapshot["today"]["pnl"]),
                "fields": [
                    discord_field("Today", snapshot["today"], True),
                    discord_field("This Week", snapshot["week"], True),
                    discord_field("This Month", snapshot["month"], True),
                    discord_field("Overall", snapshot["overall"], False),
                ],
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

    period_lines = [f"{'period':<10}{'avg CLV':>9}{'beat':>7}{'n':>6}"]
    for label, kind in (("Today", "today"), ("This Week", "week"), ("Overall", "overall")):
        s = stats_for_period(kind)
        if s.get("clv_n"):
            code = "32" if s["avg_clv"] > 0 else "31" if s["avg_clv"] < 0 else "2;37"
            period_lines.append(f"\x1b[{code}m{label:<10}{s['avg_clv']:>+9.2%}"
                                f"{s['clv_beat']:>7.0%}{s['clv_n']:>6}\x1b[0m")
        else:
            period_lines.append(f"{label:<10}{'--':>9}{'--':>7}{0:>6}")
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
                bl.append(f"\x1b[{code}m{r['bucket']:<9}{r['avg_clv']:>+9.2%}{r['clv_n']:>6}\x1b[0m")
            else:
                bl.append(f"{r['bucket']:<9}{'--':>9}{r['clv_n']:>6}")
        fields.append({
            "name": "CLV by divergence size (bigger gap should beat the close more)",
            "value": "```ansi\n" + "\n".join(bl) + "\n```",
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

    mode_badge = "🔴 LIVE" if config.LIVE else "🧪 DRY-RUN"
    payload = {
        "username": BOT_NAME,
        "embeds": [{
            "title": "📉 Polybot CLV Report",
            "description": f"{reason} — CLV = how far the market moved toward our bets by tip-off. "
                           "Consistently positive = real edge.",
            "color": 0x1ABC9C,
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
