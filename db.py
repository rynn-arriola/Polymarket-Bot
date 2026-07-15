"""SQLite position storage + reporting-period helpers, shared by
divergence_bot.py and risk.py. Schema mirrors the existing bot.py's
positions table with three additions — model_prob, market_price,
divergence — so later analysis can check whether bigger divergence actually
predicted bigger edge.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config

log = logging.getLogger("divergence_bot.db")

DB = "positions.db"

try:
    REPORT_TZ = ZoneInfo(getattr(config, "REPORT_TIMEZONE", "America/New_York"))
except ZoneInfoNotFoundError:
    # Windows Python doesn't ship the IANA tz database — pip install tzdata.
    # Loud fallback (fixed EST, no DST) so a missing package can't silently
    # shift daily-cap boundaries by an hour for half the year.
    REPORT_TZ = timezone(timedelta(hours=-5), "EST")
    log.warning(
        "zoneinfo could not find tz '%s' (tzdata package missing?) — falling back to "
        "a FIXED EST (UTC-5) offset with no DST. Run: pip install tzdata",
        getattr(config, "REPORT_TIMEZONE", "America/New_York"),
    )


def db_init():
    con = sqlite3.connect(DB)
    # WAL: readers (the `status`/reporting queries) and the writer no longer
    # block each other, so a scan-cycle INSERT can't lose to "database is
    # locked" while a report reads — that contention on the small server left
    # a REAL order untracked (2026-07-12). journal_mode is a persistent
    # property of the file, so setting it once here sticks for every later
    # connection; synchronous=NORMAL is the safe, faster pairing under WAL.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            market_slug TEXT UNIQUE,
            event_id TEXT,
            matchup TEXT,
            sport TEXT,
            side TEXT,
            price REAL,
            quantity REAL,
            stake REAL,
            live INTEGER,
            order_id TEXT,
            status TEXT,          -- pending / open / won / lost / cancelled
            settled_at TEXT,
            pnl REAL,
            model_prob REAL,       -- our Elo-derived win probability at entry
            market_price REAL,     -- Polymarket's mid price at entry (our side)
            divergence REAL,       -- model_prob - market_price at entry
            game_start TEXT,       -- event start (UTC ISO), for closing-line capture
            original_game_start TEXT, -- first known start before any exchange reschedule
            rescheduled_start TEXT,   -- latest exchange-confirmed postponed start
            rescheduled_at TEXT,      -- when the bot first/last marked it rescheduled
            is_long INTEGER,       -- 1 if we bought the long side (to price the same side at close)
            closing_price REAL,    -- market price for our side captured near game start (CLV)
            closing_captured_at TEXT,
            pnl_reconciled INTEGER DEFAULT 0  -- 1 once live pnl reflects the exchange's
                                              -- real fee-inclusive figure, not our estimate
        )"""
    )
    # Migration: add newer columns to a positions table created before they
    # existed. ALTER only runs for columns the table doesn't already have, so
    # this is a no-op on fresh installs and safe to run every startup.
    have = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    for name, decl in (("game_start", "TEXT"), ("is_long", "INTEGER"),
                       ("original_game_start", "TEXT"), ("rescheduled_start", "TEXT"),
                       ("rescheduled_at", "TEXT"),
                       ("closing_price", "REAL"), ("closing_captured_at", "TEXT"),
                       ("pnl_reconciled", "INTEGER DEFAULT 0"),
                       ("cancel_verified", "INTEGER DEFAULT 0"),
                       # Manual (hand-placed) activity the operator does on a
                       # BOT position: contracts they sold/covered by hand
                       # (a partial or full cash-out), contracts they added by
                       # hand, the P&L those hand exits banked, and why the row
                       # closed. manual_sync.detect_manual_cashouts owns these.
                       ("manual_sold_qty", "REAL DEFAULT 0"),
                       ("manual_added_qty", "REAL DEFAULT 0"),
                       ("cashout_pnl", "REAL DEFAULT 0"),
                       ("close_reason", "TEXT")):
        if name not in have:
            con.execute(f"ALTER TABLE positions ADD COLUMN {name} {decl}")
    con.execute(
        """CREATE TABLE IF NOT EXISTS daily_state (
            day TEXT PRIMARY KEY,
            open_balance REAL
        )"""
    )
    # Separate ledger for bets entered outside the bot. This intentionally
    # does not reuse `positions`: bot accounting has a unique market_slug
    # guard and exchange-driven settlement flow, while manual tracking needs
    # to allow cashouts, cancelled orders, and multiple human-entered bets on
    # the same market without affecting the live bot's duplicate guards.
    con.execute(
        """CREATE TABLE IF NOT EXISTS manual_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            market_slug TEXT,
            matchup TEXT,
            sport TEXT,
            side TEXT,
            price REAL,
            quantity REAL,
            stake REAL,
            live INTEGER DEFAULT 1,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            close_price REAL,
            pnl REAL,
            close_reason TEXT,
            notes TEXT
        )"""
    )
    # Migration: manual_trades needs is_long so manual_sync can record whether
    # a hand-placed bet is a long (bought) or short (sold-to-open) position —
    # the exchange nets both, and a short's cash math is the mirror of a long's.
    have_manual = {r[1] for r in con.execute("PRAGMA table_info(manual_trades)")}
    if "is_long" not in have_manual:
        con.execute("ALTER TABLE manual_trades ADD COLUMN is_long INTEGER DEFAULT 1")
    # Paper-only ledger for every valid model signal, including signals the
    # live risk policy declines. It must stay separate from `positions`: no
    # signal can consume bankroll, open slots, or affect real P&L/reporting.
    con.execute(
        """CREATE TABLE IF NOT EXISTS shadow_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            market_slug TEXT UNIQUE NOT NULL,
            event_id TEXT,
            matchup TEXT,
            sport TEXT,
            side TEXT,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            stake REAL NOT NULL,
            live INTEGER NOT NULL,
            decision TEXT NOT NULL DEFAULT 'candidate', -- traded / not_traded / order_failed
            decision_reason TEXT,
            status TEXT NOT NULL DEFAULT 'open',        -- open / won / lost / push
            model_prob REAL,
            market_price REAL,
            divergence REAL,
            game_start TEXT,
            is_long INTEGER,
            closing_price REAL,
            closing_captured_at TEXT,
            settlement_price REAL,
            estimated_fee REAL,
            paper_pnl REAL,
            settled_at TEXT,
            last_settlement_check_at TEXT
        )"""
    )
    con.commit()
    con.close()


def db(query, args=(), fetch=False):
    # timeout=30: a concurrent `status` read shouldn't fail with "database is
    # locked" right after a live order was already sent to the exchange.
    con = sqlite3.connect(DB, timeout=30)
    cur = con.execute(query, args)
    rows = cur.fetchall() if fetch else None
    con.commit()
    con.close()
    return rows


def today() -> str:
    return datetime.now(REPORT_TZ).date().isoformat()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _local_midnight(d) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=REPORT_TZ)


def period_bounds_utc(kind: str) -> tuple[str, str]:
    """UTC timestamp range for a reporting-timezone period."""
    now_local = datetime.now(REPORT_TZ)
    if kind == "today":
        start = _local_midnight(now_local.date())
        end = start + timedelta(days=1)
    elif kind == "week":
        start = _local_midnight(now_local.date() - timedelta(days=now_local.date().weekday()))
        end = start + timedelta(days=7)
    elif kind == "month":
        start = datetime(now_local.year, now_local.month, 1, tzinfo=REPORT_TZ)
        if now_local.month == 12:
            end = datetime(now_local.year + 1, 1, 1, tzinfo=REPORT_TZ)
        else:
            end = datetime(now_local.year, now_local.month + 1, 1, tzinfo=REPORT_TZ)
    else:
        raise ValueError(f"unsupported period kind: {kind}")
    return _utc_iso(start), _utc_iso(end)


def day_open_balance(effective_bankroll: float, persist: bool = True) -> float:
    """Balance snapshotted at the start of today (reporting timezone), held
    fixed all day — the sizing bankroll and the daily-loss-limit baseline.

    Keyed by day AND mode (live vs dry-run): the two must never share a
    snapshot, or a dry-run day-open (e.g. $1000 simulated) would leak into a
    live run started later the same day and mis-size real orders.

    persist=False reads the existing snapshot (or returns the passed value)
    WITHOUT writing one — used when the value is an estimate (e.g. the
    cold-start BANKROLL fallback while the balance API is down), so a guess
    can never get locked in as the whole day's sizing baseline."""
    key = f"{today()}:{'live' if config.LIVE else 'dry'}"
    rows = db("SELECT open_balance FROM daily_state WHERE day=?", (key,), fetch=True)
    if rows:
        return rows[0][0]
    if persist:
        db("INSERT OR REPLACE INTO daily_state (day, open_balance) VALUES (?,?)", (key, effective_bankroll))
    return effective_bankroll
