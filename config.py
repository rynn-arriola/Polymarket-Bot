# ============================================================
# DIVERGENCE BOT CONFIG — strategy/tuning knobs (safe to commit).
# Secrets live in credentials.py (gitignored) or environment
# variables — see credentials.example.py. Never paste them here.
# ============================================================
import os
from datetime import date

try:
    import credentials as _cred
except ImportError:
    _cred = None


def _secret(name: str, default: str = "") -> str:
    """Env var first (server/CI friendly), then credentials.py, then default.
    Defaults keep the existing 'PASTE_YOUR' startup guard working when
    neither source provides a value."""
    return os.environ.get(name) or getattr(_cred, name, "") or default


# --- API credentials (from polymarket.us/developer) ---
# Set in credentials.py (copy credentials.example.py) or as env vars.
KEY_ID = _secret("KEY_ID", "PASTE_YOUR_KEY_ID")
SECRET_KEY = _secret("SECRET_KEY", "PASTE_YOUR_SECRET_KEY")

# --- KILL SWITCH ---
# False = dry-run: bot logs every order it WOULD place, no real money.
# True  = live: real orders with real dollars.
# Run dry for a good stretch first — this strategy has never traded before,
# unlike the existing band-filter bot. Judge nothing before ~100 settled
# positions AND a `python backtest.py` calibration table that looks sane.
LIVE = True

# --- Strategy: divergence entries ---
# Our own Elo-derived win probability vs. Polymarket's current mid price
# (average of best bid/ask). Only trade when they disagree by at least the
# sport's threshold below — the core "edge" this bot is built around.
#
# Thresholds are PER SPORT, derived from the backtest (2026-07-08): each
# sport's threshold = its model's measured calibration noise floor (weighted
# |predicted - actual| across probability buckets: ATP 0.9%, WTA 1.3%, NBA
# 1.5%, WNBA 3.2%, FWC 5.2%, MLB 0.4%) + worst-case taker fee (~1.2%) + a
# margin for the market being partly right when we disagree. The margin is
# biggest for MLB (the most heavily modeled sport there is — our model
# being calibrated doesn't mean it beats THAT market) and FWC (World Cup
# gets sharp attention). A uniform 5% was wrong on both ends: below FWC's
# own noise floor, needlessly loose for ATP.
DIVERGENCE_THRESHOLD = 0.05          # fallback for sports not listed below
DIVERGENCE_THRESHOLDS = {
    "atp": 0.045,
    "wta": 0.05,
    "nba": 0.05,
    "wnba": 0.065,
    "mlb": 0.065,
    "fwc": 0.085,
    "itf": 0.06,   # no backtest possible yet (no data source) — conservative
    # Esports: measured noise floors (2026-07-08: dota2 2.6%, cs2 0.8%,
    # valorant 3.7% on thin data) + fees + an extra roster-blindness margin
    # — team-level Elo can't see roster changes, esports' biggest swing
    # factor, so these stay deliberately above the bare floor+fee math.
    "dota2": 0.07,
    "cs2": 0.06,
    # LoL now measured (2026-07-09: noise floor 1.9% on 15k cached matches,
    # tuned k=72, Brier 0.219) + fee/margin; the roster guard covers the
    # churn risk the placeholder padding used to stand in for.
    "lol": 0.05,
    "valorant": 0.08,
}

# Guard rails at the extremes: a small modeling error near 0% or 100% turns
# into a huge relative edge, which is exactly where an Elo model (or the
# name-matching that feeds it) is most likely to be silently wrong. Skip
# trading outside this band even if the raw divergence clears the threshold.
PRICE_FLOOR = 0.05
PRICE_CEIL = 0.95

# Adverse-selection guard: a divergence LARGER than this is treated as "the
# market knows something we don't" (star scratched, lineup news, weather)
# rather than "free money", and skipped. Sharp money moves prediction
# markets on news within minutes; our model updates on results only. The
# biggest edges are the most suspicious ones.
MAX_DIVERGENCE = 0.20

# --- Key-player injury filter (NBA/WNBA, ESPN feeds) ---
# Skip a market when either team has one of its top INJURY_TOP_N scorers
# (by PPG) listed with a status below. An Elo rating reflects the roster
# that earned it — rather than guess how many points a missing star is
# worth, the bot declines to trade games where the roster materially
# differs from the one it rated. "Day-To-Day"/"Questionable" players
# usually end up playing, so those statuses don't trigger a skip.
# MLB is covered separately (the probable STARTER is tracked directly, and
# a late scratch is picked up when the 30-min pitcher cache refreshes);
# tennis needs no filter (if the match starts, both are playing); FWC has
# no free lineup feed — the late entry window + MAX_DIVERGENCE are the
# mitigation there.
INJURY_FILTER = True
INJURY_TOP_N = 3
INJURY_SKIP_STATUSES = ("Out", "Doubtful")

# --- Roster-change guard (esports) ---
# The esports analogue of the injury filter. An Elo rating is earned by a
# specific roster, but follows the team NAME — when a team swaps players the
# rating is stale and the market (which reprices on the news instantly) will
# look like a fat edge. The guard snapshots each team's current lineup and
# skips its markets when the roster has changed by >= ROSTER_CHANGE_MIN_DIFF
# players, until the team has played ROSTER_REACCEPT_MATCHES games under the
# new lineup (Elo re-equilibrated). Providers: Dota2 (OpenDota), Valorant
# (vlr), LoL (Leaguepedia). CS2 has no free roster source, so the guard
# no-ops there and only its divergence threshold protects it. Fail-open: an
# unfetchable roster never blocks trading (see elo/rosters.py).
# --- LoL player-level Elo (Oracle's Elixir) ---
# Backtest-validated: player-Elo beat team-Elo on identical 2023 games
# (Brier 0.2253 vs 0.2300). It goes live for LoL automatically WHEN the
# lol_player_model.json sidecar exists AND its data is fresher than
# LOL_PLAYER_FRESHNESS_DAYS (else team-Elo stays live — 2023-only data
# would carry stale lineups). Refresh data with `python fetch_oe.py`
# (Oracle's Elixir; Google Drive quota resets ~daily) then rebuild.
LOL_PLAYER_ELO = True
LOL_PLAYER_FRESHNESS_DAYS = 45

# --- PandaScore (Valorant source upgrade) ---
# Valorant's default source is a flaky, scrape-backed vlr.gg mirror with only
# relative dates and ~13 months of depth. PandaScore's free "Fixtures Only"
# plan gives structured Valorant past-match results with REAL dates and
# deeper history (1000 requests/hour). Get a free token at
# https://pandascore.co (Sign up -> Dashboard -> API token) and paste it
# here. Blank = fall back to the vlr.gg mirror (no behavior change).
# On the first run WITH a token, the accumulating store cuts over to
# PandaScore automatically (old mirror-sourced entries are dropped so the
# two sources can't double-count the same match under different ids).
PANDASCORE_TOKEN = _secret("PANDASCORE_TOKEN")

# Which titles use PandaScore for match data AND rosters. The same free
# token also covers dota2 and cs2 (slug "csgo") with 3-6x deeper history
# than the keyless sources (verified 2026-07-12: dota2 ~40k matches to
# 2015, csgo ~95k to 2016, valorant ~18k to 2021) — but a title is only
# promoted into this tuple after the deeper data BEATS the old source in
# a walk-forward backtest. Promoting cs2 also activates its first-ever
# roster guard (PandaScore is the first usable CS2 roster source).
PANDASCORE_TITLES = ("valorant", "dota2", "cs2")

# --- XGBoost gated layer (future hook) ---
# A per-sport XGBoost model can TAKE OVER a sport's probability, but only if it
# beat Elo out-of-sample (train_xgb.py writes beats_elo into the model's meta)
# AND was trained within XGB_STALE_DAYS. With no model files present, every
# sport stays on Elo — this is off by default and changes nothing until you
# train a model that earns its way on (e.g. after a new data source lands).
XGB_ENABLED = True
XGB_STALE_DAYS = 45

ROSTER_GUARD = True
# Change is measured as added + removed players between snapshots, so a
# single 1-for-1 substitution counts as 2. Default 2 therefore triggers on
# ANY real roster swap, while tolerating a provider that transiently returns
# an incomplete roster (one player merely missing = 1 = no false trigger).
ROSTER_CHANGE_MIN_DIFF = 2
ROSTER_REACCEPT_MATCHES = 15   # games under the new lineup before trusting the rating again

# Polymarket league code -> this project's internal sport key (must have a
# matching cached engine in elo_ratings.json, built by build_ratings.py).
# Anything not listed here is skipped before any market data is even
# fetched for it. Esports coverage (added 2026-07-08) uses free keyless
# sources: OpenDota (Dota 2), bo3.gg (CS2), Leaguepedia (LoL), and a
# vlr.gg mirror (Valorant) — see elo/esports.py for caveats, especially
# roster changes, which team-level Elo can't see.
SUPPORTED_SPORTS = {
    "MLB": "mlb",
    "NBA": "nba",
    "WNBA": "wnba",
    "ATP": "atp",
    "WTA": "wta",
    "ITFME": "itf",
    "ITFWO": "itf",
    "FWC": "fwc",
    "DOTA2": "dota2",
    "CS2": "cs2",
    "LOL": "lol",
    "VALORANT": "valorant",
}

# Competitions to SKIP even though they carry a league code we rate. Our Elo
# ratings are built from REGULAR-season games, so a market that shares the
# league code and team NAMES but uses entirely different rosters produces a
# meaningless probability. The prime example: NBA Summer League (Polymarket
# slug "aec-nbasl-...", played in July) — same team names, all rookies/fringe
# players. Any market whose slug contains one of these substrings (case-
# insensitive) is skipped before it can be traded. Extend as needed
# (e.g. preseason indicators). Regular NBA slugs are "aec-nba-...", which do
# NOT contain "nbasl", so real games are unaffected.
EXCLUDE_MARKET_SLUGS = ("nbasl",)

# --- Market filters (liquidity / data quality — same rationale as bot.py) ---
PREGAME_ONLY = True        # this bot only trades pregame; an Elo model built
                             # from historical results has no opinion on
                             # in-game state, so live (in-play) games are out
                             # of scope here, unlike the existing bot.py.
TRADE_BEFORE_START_MINUTES = 60  # only enter during the final hour before game
                             # start — late on purpose: injury reports and
                             # starting lineups are public by then, so the
                             # market has already priced roster news and a
                             # divergence is likelier to be a real mispricing
                             # than the market knowing about a scratch we
                             # can't see. Liquidity is also best right
                             # before start.
MIN_OPEN_INTEREST = 100     # skip markets with fewer than this many contracts of open interest
MAX_SPREAD_USD = 0.08       # skip markets where (ask - bid) exceeds this — with no fixed
                             # price band to keep us near the edges, a wide spread on a
                             # mid-priced market is still a sign of a thin, unreliable book

# --- Bankroll & risk (hard limits, enforced in code) ---
# Conservative on purpose: this strategy is unproven. Tighten/loosen once
# `status` has real settled-position history to judge by.
BANKROLL = 700.00            # cold-start fallback (LIVE only): used only if the very
                             # first balance fetch fails before any real balance is cached
DRY_RUN_BANKROLL = 1000.00   # fixed simulated bankroll for DRY-RUN, decoupled from real balance
# Per-bet stake is quarter-Kelly by edge (or flat STAKE_PCT if Kelly is off),
# then clamped to a PERCENT-OF-BANKROLL cap — so position size tracks the
# account up and down instead of being pinned to a fixed dollar amount.
# MIN_STAKE stays an ABSOLUTE floor: contracts are whole units priced
# $0.05-$0.95, so a sub-$1 stake can buy zero of them.
STAKE_PCT = 0.01             # flat sizing: 1% of bankroll per bet (ACTIVE — Kelly off below)
KELLY_FRACTION = 0           # 0 = Kelly OFF, every bet is a flat STAKE_PCT of bankroll.
                             # (Was 0.25 quarter-Kelly until 2026-07-12 — edge-scaled bets
                             # kept hitting the 2% MAX_STAKE_PCT cap; restore 0.25 to re-enable.)
MAX_STAKE_PCT = 0.02         # hard cap: never risk more than 2% of bankroll on one bet
MIN_STAKE = 1.00             # absolute $ floor (whole-contract minimum)
MAX_OPEN_POSITIONS = 10
MAX_PER_SPORT_PER_DAY = 5    # correlation cap: max entries per sport per day
DAILY_LOSS_LIMIT_PCT = 0.06  # halt new entries after losing this % of the day's starting balance
FEE_BUFFER_PCT = 0.005       # cash held back (0.5% of bankroll) so fees can't push an order over balance

# --- Live-order safety ---
# Short-side entries (buying the underdog/No side) settle via the inverse of
# the market's long-side settlement — handled by side-aware check_settlements,
# but not yet validated against a REAL settled short order. As extra insurance
# for a first live run, set LONG_ONLY = True to take only long-side entries
# (proven settlement math), then flip it off once a live short has settled
# correctly. In dry-run it simply skips short candidates.
LONG_ONLY = False
# If an open position hasn't settled this many days after entry AND settlement
# checks keep failing, warn (throttled) — surfaces a broken/auth-expired
# settlement endpoint instead of silently never settling anything.
SETTLEMENT_STUCK_WARNING_DAYS = 14
# The exchange RESTATES a resolution's cost basis (rolls fees in) shortly
# after posting it — P&L read earlier sticks slightly optimistic (audited
# 2026-07-13: 16 rows, ~$10.6 overstated). Settlement still fires instantly,
# but the figure is only stamped FINAL once the resolution activity is at
# least this old; until then reconcile_live_pnl keeps refreshing it.
RESOLUTION_STABLE_MINUTES = 45

# --- Discord status updates ---
# Optional: paste a Discord channel webhook URL to receive bot summaries
# (status every DISCORD_STATUS_INTERVAL_MIN minutes + a once-daily digest
# with per-sport records and divergence-bucket edge validation). You can
# reuse the webhooks from the other bot's config.py — both bots' messages
# will then share a channel, distinguished by username — or make a fresh
# channel + webhook to keep this bot's reporting separate. Keep URLs
# private: anyone with one can post into that channel.
DISCORD_WEBHOOK_URL = _secret("DISCORD_WEBHOOK_URL")
DISCORD_STATUS_INTERVAL_MIN = 5

# Optional: a second webhook for one polished message per settled position
# (includes what the model believed vs what the market charged at entry).
DISCORD_SETTLEMENT_WEBHOOK_URL = _secret("DISCORD_SETTLEMENT_WEBHOOK_URL")

# Optional: a dedicated webhook for the CLV (closing-line value) report — the
# fastest read on whether the edge is real (does the market move toward our
# bets by tip-off?). Posted on its own channel, CLV_REPORT_TIMES_PER_DAY times
# a day (4 = every 6h). Blank = no CLV report posted.
DISCORD_CLV_WEBHOOK_URL = _secret("DISCORD_CLV_WEBHOOK_URL")
CLV_REPORT_TIMES_PER_DAY = 4

# Optional: a dedicated OPS/ERRORS webhook — the "needs developer attention"
# channel. Every WARNING and above the bot logs (API failures, stale
# ratings/models, unrecognized data formats, untracked orders, settlement
# problems) is forwarded here automatically, deduplicated and batched every
# ERROR_ALERT_BATCH_MINUTES; ERROR/CRITICAL flush within ~a minute. Same
# content as divergence_bot.errors.log / `python divergence_bot.py errors`,
# pushed instead of pulled. Blank = disabled.
DISCORD_ERRORS_WEBHOOK_URL = _secret("DISCORD_ERRORS_WEBHOOK_URL")
ERROR_ALERT_BATCH_MINUTES = 10
# Once a day (after this hour, reporting timezone) the same channel gets an
# OPS DIGEST: the day's problems grouped into categories with a what-to-fix
# hint each — a ranked to-do list distilled from all of the day's alerts.
OPS_DIGEST_HOUR = 18   # 6pm ET

# --- Reporting timezone ---
REPORT_TIMEZONE = "America/New_York"

# --- Timing ---
SCAN_INTERVAL_SECONDS = 60
CANCEL_UNFILLED_AFTER_MIN = 10

# --- Rescheduled-match exit ---
# A filled position whose match gets POSTPONED holds a broken premise: entry
# was priced on conditions an hour before a match that now plays days later
# (lineups/meta can change), while capital sits locked. Once a position is
# this many hours past its ORIGINAL start, the bot re-checks the market's
# own gameStartTime; if the exchange has moved the start INTO THE FUTURE
# (its word — never elapsed time, so a long game or a short rain delay can
# never trigger this), the position is closed at market and booked from the
# actual fills. 0 disables the feature.
RESCHEDULE_EXIT_AFTER_HOURS = 2

# --- Closing-line value (CLV) capture ---
# The single fastest read on whether the strategy has real edge: for each open
# position, snapshot the market's price for our side in the final minutes
# before tip-off (the "closing line"), then compare it to our entry price. If
# the market consistently moves TOWARD our bets by close, the edge is real —
# this converges far faster than settled P&L. The bot re-snapshots each cycle
# inside this window, so the last value before start is the true closing line.
CLOSING_CAPTURE_MINUTES = 5

# --- Self-refresh (24/7 server operation) ---
# The running bot rebuilds all ratings this often (hours) by spawning
# refresh_data.py in the background and hot-reloading the result — no cron,
# no restart, no pause in trading. 6h = 4x/day. Live in-game data (injuries,
# pitchers, rosters, tennis surface) refreshes separately on its own short
# TTLs every cycle, independent of this.
DATA_REFRESH_HOURS = 6

# --- Ratings freshness guard ---
# build_ratings writes elo_freshness.json (per-sport last_built time + latest
# game date). If a sport's ratings haven't successfully rebuilt within this
# many hours, the bot STOPS trading that sport — a refresh that silently fails
# (source down, rate limit, OOM-kill on the small droplet) otherwise leaves it
# trading on stale ratings with no warning. At DATA_REFRESH_HOURS=6 this is
# ~4 missed refreshes. Fail-open: a missing freshness file never blocks trading
# (so an old deployment predating this file keeps working). 0 disables the guard.
RATINGS_STALE_HOURS = 24

# --- Elo build ranges (used by build_ratings.py / backtest.py) ---
# MLB Stats API pulls a whole season in one call, so a deep range is cheap.
MLB_START_YEAR = 2022
# ESPN's scoreboard serves month-range calls (cached, immutable once past),
# so deep history is a one-time cost then free. Verified live (2026-07-10)
# that ESPN carries NBA back to 2014, WNBA to 2018, WTA tournaments to 2019.
# Ranges chosen to feed a future gradient-boosting layer: ~10k NBA games,
# all available WNBA, ~5 seasons of WTA — while staying recent enough that
# season regression keeps ancient rosters from dominating.
NBA_START_DATE = date(2019, 10, 1)
WNBA_START_DATE = date(2018, 5, 1)

# WTA is built from ESPN's tennis scoreboard over this date range. ATP uses
# TML-Database CSVs instead (deep history + real surface labels), from
# TENNIS_TML_START_YEAR — see elo/tennis.py. TML has no WTA mirror (verified
# 2026-07-10: all WTA CSV name variants 404), so WTA stays on ESPN.
TENNIS_START_DATE = date(2019, 1, 1)
TENNIS_TML_START_YEAR = 2019

# --- ITF tennis data (fallback only — ESPN doesn't cover ITF) ---
# See README.md: ESPN has no ITF endpoint, and Jeff Sackmann's public
# ITF match-history CSVs were unreachable when this was built — place
# current CSVs (tourney_date/winner_name/loser_name columns, one row per
# match) in data/tennis/itf/ if you find a source, otherwise ITF markets
# are simply skipped (no model).
