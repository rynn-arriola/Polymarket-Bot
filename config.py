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
# 2026-07-20: paper mode by operator call — no real money in the account for
# now. The live order path stays intact; flip back to True to resume trading.
LIVE = False

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

# Every candidate that clears the model and market-quality guards in the
# PRICE_FLOOR..PRICE_CEIL range is recorded in the paper-only signal ledger.
# Live orders are deliberately stricter: the 5%-30% range is observed and
# settled on paper, but never reaches the risk gate or exchange. Use the
# ledger reports to decide later whether this live floor should move.
LIVE_ENTRY_PRICE_FLOOR = 0.30
LIVE_ENTRY_PRICE_CEIL = PRICE_CEIL
TRACK_ALL_VALID_SIGNALS = True
# Same conservative per-contract fee allowance used by the price-band report.
# It is an estimate only; no exchange fee or balance is ever affected.
SIGNAL_PAPER_FEE_PER_CONTRACT = 0.012
# Public settlement is polled less often for paper signals so analytics cannot
# add unnecessary API load to the live trading loop.
SIGNAL_SETTLEMENT_CHECK_INTERVAL_MIN = 10

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

# Valorant follows the same player-blend contract as LoL. Its bulk archive
# has monotonic VLR match IDs but no dates, so freshness uses Kaggle's public
# dataset `lastUpdated` timestamp. Stale/missing state falls back to team Elo.
VALORANT_PLAYER_FRESHNESS_DAYS = 45

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
    "MLB": "mlb",  # re-enabled 2026-07-18 on the ESPN source (statsapi.mlb.com
                   # blocks the droplet IP; see elo/mlb.py + OPERATOR.md §11)
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
STAKE_PCT = 0.0025           # flat sizing: 0.25% of bankroll per bet (ACTIVE — Kelly off below).
                             # Reduced from 1% on 2026-07-18 (operator call): still in the
                             # data-gathering phase — CLV hasn't validated the edge yet, so keep
                             # real-money exposure minimal while the sample accumulates. Note at
                             # ~$555 bankroll this is ~$1.39/bet; below ~$400 bankroll the $1
                             # MIN_STAKE floor takes over and sizing is effectively flat $1.
KELLY_FRACTION = 0           # 0 = Kelly OFF, every bet is a flat STAKE_PCT of bankroll.
                             # (Was 0.25 quarter-Kelly until 2026-07-12 — edge-scaled bets
                             # kept hitting the 2% MAX_STAKE_PCT cap; restore 0.25 to re-enable.)
MAX_STAKE_PCT = 0.02         # hard cap: never risk more than 2% of bankroll on one bet
MIN_STAKE = 1.00             # absolute $ floor (whole-contract minimum)
MAX_OPEN_POSITIONS = 10
MAX_PER_SPORT_PER_DAY = 5    # correlation cap: max entries per sport per day
DAILY_LOSS_LIMIT_PCT = 0.02  # halt new entries after losing this % of the day's starting balance.
                             # Scaled down from 6% on 2026-07-18 alongside STAKE_PCT 1%->0.25%
                             # (operator picked 2% over a strict /4=1.5%): brake trips after ~8
                             # full losses at the smaller stakes — at 6% it would have taken ~24.
                             # Restore to 0.06 together with STAKE_PCT = 0.01.
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
# Paper-signal tracking embed (fires alongside every status update above).
# Blank = post to DISCORD_WEBHOOK_URL as its own separate message; set a
# webhook here to route the paper ledger to its own channel instead.
DISCORD_PAPER_WEBHOOK_URL = _secret("DISCORD_PAPER_WEBHOOK_URL")

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

# --- API rate-limit protection (added after the 2026-07-16 Cloudflare ban) ---
# The full active-market catalog (~9,000 markets = ~90 paginated calls) was
# refetched in a tight burst EVERY cycle — the dominant API load by far, and
# what got the droplet temporarily banned (Cloudflare 1015). The catalog is
# only DISCOVERY metadata (slugs, teams, start times); entry prices always
# come from a fresh per-market bbo call each cycle, so the catalog can be
# refreshed slowly and reused in between without staling any price.
# THE structural defense (added 2026-07-17 after the ban loop): a global
# token-bucket governor in api_guard.pace() that EVERY exchange HTTP call
# passes through (wired at client construction via api_guard.governed()).
# Worst case = SUSTAINED*60 + BURST calls in any minute (~100 at defaults) —
# under the rate that even quiet-hour catalog bursts survived, so the bot
# cannot exceed the budget no matter how busy a match window gets; calls
# queue for a moment instead of bursting. 0 disables (don't).
API_SUSTAINED_CALLS_PER_SEC = 1.5
API_BURST_CALLS = 10
MARKET_LIST_REFRESH_MIN = 10   # catalog refetch cadence (0 = refetch every cycle)
MARKET_LIST_MAX_AGE_MIN = 60   # if refetches keep FAILING, stop serving a catalog
                               # older than this (skip discovery; stale start
                               # times could mislabel a postponed match pregame)
MARKET_PAGE_SPACING_SEC = 1.0  # pause between catalog pagination calls.
                               # 0.25 (a ~115-call drip over ~30s, ~4 req/s)
                               # still tripped Cloudflare on 2026-07-17 whenever
                               # a busy match window stacked settlement/CLV
                               # calls on top; 1.0 spreads the catalog over
                               # ~2 min at ~1 req/s, well under the limit.
# Catalog page size and server-side category filter (probed 2026-07-17):
# the API serves up to 500 markets/page (hard cap — asking for more still
# returns 500, which is why fetch_all_markets clamps to 500: a full server
# page would otherwise look like a final short page and silently truncate
# the catalog) and honors categories=["sports"] server-side. Together they
# shrink the refetch from ~115 calls to ~20. Empty MARKET_CATEGORIES = no
# filter (fetch everything, as before).
MARKET_PAGE_SIZE = 500
MARKET_CATEGORIES = ("sports",)
# If the exchange answers with a Cloudflare BAN page anyway, pause all
# exchange calls this long so the ban can expire — hammering through resets
# Cloudflare's rolling window and extends it. Pausing is safe: fills,
# settlements, and reconciliation all catch up when calls resume (proven
# during the 3h ban on 2026-07-16 — zero money lost).
API_BAN_COOLDOWN_MIN = 5
# Repeated bans escalate: each ban detected within RECENT_MIN of the previous
# one doubles the pause (5 -> 10 -> 20 -> capped at MAX_MIN), because a re-ban
# right after resuming means Cloudflare's window is still hot and 5 min flat
# just re-triggers it (11 ban episodes in 2h on 2026-07-17). A ban later than
# RECENT_MIN after the last one is a fresh incident and starts back at 5.
API_BAN_COOLDOWN_MAX_MIN = 40
API_BAN_RECENT_MIN = 15
# After a ban ends, the full catalog refetch (the heaviest burst we make) is
# the LAST thing to send at a still-warm rate limiter — it's what re-tripped
# the ban again and again on 2026-07-17 (cache expires during the pause, so
# the first post-resume cycle refired all ~115 pages and got re-banned in
# seconds). Keep serving the cached catalog this long after any ban before
# allowing a refetch (bounded by MARKET_LIST_MAX_AGE_MIN as always).
POST_BAN_CATALOG_GRACE_MIN = 10
# A settled row whose POSITION_RESOLUTION never appears on the activity feed
# (exchange quirk — a won MLB row from 2026-07-12 and a voided dota2 push
# both never got one) can never reconcile; without a give-up, each such row
# polls the feed every cycle FOREVER (1,440 calls/day each). After this many
# days the estimated P&L is kept as final. 0 disables the give-up.
RECONCILE_GIVE_UP_DAYS = 3

# --- Rescheduled-match hold marker ---
# A filled position whose match gets POSTPONED stays open so we don't pay an
# early-exit fee just to free a slot. Once a position is this many hours past
# its ORIGINAL start, the bot re-checks the market's own gameStartTime; if the
# exchange has moved the start INTO THE FUTURE (its word — never elapsed time,
# so a long game or a short rain delay can never trigger this), the bot marks
# the position as rescheduled, updates game_start to the new start for CLV, and
# lets the open-position cap expand by that marked count. 0 disables the check.
RESCHEDULE_MARK_AFTER_HOURS = 2
# Backward-compatible name for older local/server configs.
RESCHEDULE_EXIT_AFTER_HOURS = RESCHEDULE_MARK_AFTER_HOURS

# --- Closing-line value (CLV) capture ---
# The single fastest read on whether the strategy has real edge: for each open
# position, snapshot the market's price for our side in the final minutes
# before tip-off (the "closing line"), then compare it to our entry price. If
# the market consistently moves TOWARD our bets by close, the edge is real —
# this converges far faster than settled P&L. The bot re-snapshots each cycle
# inside this window, so the last value before start is the true closing line.
CLOSING_CAPTURE_MINUTES = 5
# If the pre-start window was missed entirely (bot down, Cloudflare ban
# cooldown — those escalate up to 40 min), a single post-start price is still
# captured up to this many minutes after start, so the trade isn't silently
# absent from CLV forever. A late capture is in-play-tainted (the price has
# started drifting toward 0/1), which is why this is a fallback and the
# pre-start snapshot always wins when it exists. Must outlast the longest ban
# cooldown (API_BAN_COOLDOWN_MAX_MIN = 40) or a ban spanning tip-off loses
# the sample.
CLOSING_FALLBACK_MINUTES = 60

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
# See OPERATOR.md: ESPN has no ITF endpoint, and Jeff Sackmann's public
# ITF match-history CSVs were unreachable when this was built — place
# current CSVs (tourney_date/winner_name/loser_name columns, one row per
# match) in data/tennis/itf/ if you find a source, otherwise ITF markets
# are simply skipped (no model).
