# Divergence Bot — Polymarket US, Elo-vs-market-price entries

Standalone from the existing `bot.py` project — separate folder, separate
credentials, separate `positions.db`/log. Nothing here touches that project.

Instead of trading a fixed price band, this bot computes its own win
probability per matchup from an Elo rating system built on historical
results, compares it to Polymarket's current price, and only enters when the
two disagree by ≥5 percentage points (`config.DIVERGENCE_THRESHOLD`) — on
whichever side (favorite or underdog) is actually underpriced.

## Sport support matrix

| Sport | Data source | Status |
|---|---|---|
| MLB | MLB Stats API (free, no key) | Working, but weak (see calibration note below) |
| NBA / WNBA | ESPN public scoreboard (free, no key) | Working, well-calibrated |
| ATP tennis | TML-Database CSVs (free, no key) | Working — deep history (2019+) with real surface labels |
| WTA tennis | ESPN public scoreboard (free, no key) | Working, well-calibrated (no live TML-style mirror found for WTA) |
| FIFA World Cup (Polymarket "FWC") | ESPN public scoreboard | Working, best-effort (thin sample — see note below) |
| ITF tennis | Local CSV files (Sackmann schema) | **Not covered by ESPN — requires manual data download, see below** |
| Dota 2 | OpenDota `/proMatches` (free, keyless) | Working — ~7 months of history and deepening (see note) |
| CS2 | bo3.gg public API (free, keyless) | Working — full year, tiers S–C |
| League of Legends | Leaguepedia Cargo API (free, keyless, heavily rate-limited) | Working — full year, all pro leagues |
| Valorant | vlr.gg mirror (free, keyless, flaky) | Working — ~5 months and deepening (see note) |

**Esports notes (added 2026-07-08).** None of these need the Riot developer
portal or a STRATZ key (those cover ranked/player data, not pro match
results). Several sources only expose a sliding window of recent results,
so each title keeps an *accumulating* store (`data/cache/esports_*.json`)
that deepens with every `build_ratings.py` run — another reason to run it
daily. Two esports-specific caveats:
- **Roster changes are the model's blind spot**: an Elo rating follows the
  team NAME, and a rebuilt roster inherits the old tag's rating. High
  K-factors and deliberately raised divergence thresholds compensate, but
  imperfectly. On top of those, a **roster-change guard** (`elo/rosters.py`)
  snapshots each team's current lineup and *skips* its markets when the
  roster changes by 2+ players (a real substitution), until the team has
  played `ROSTER_REACCEPT_MATCHES` games under the new lineup and the rating
  has re-equilibrated. Roster providers per title, each its own free source:
  Dota 2 (OpenDota), Valorant (vlr.gg), LoL (Leaguepedia). **CS2 has no free
  roster source** (bo3's team filter is broken, HLTV blocks scraping), so the
  guard no-ops there — CS2 relies on its divergence threshold alone. The
  guard is *fail-open*: an unfetchable roster never blocks trading, since the
  endpoints are flaky and failing closed would halt most esports trading.
  This is a stopgap, not a true player model — the rating still can't see
  *who* is playing, only that the lineup changed (see "player-level Elo" in
  the roadmap).
- Polymarket lists part of its esports schedule (some Dota 2 events) as
  paired single-team Yes/No markets — same structure as FWC — handled by
  the shared paired-market path, including the guard that prevents buying
  Yes on both teams of the same match through their two separate slugs.

**Calibration results** (`backtest.py`, walk-forward — every prediction uses
only ratings built from strictly earlier games, the same information the
live bot would have had). Brier score: lower is better, 0.25 = coin flip.

| Sport | Brier (v1, plain Elo) | Brier (v2, tuned + upgrades) | What changed |
|---|---|---|---|
| NBA | 0.2153 | **0.2094** | margin-of-victory scaling, back-to-back rest penalty (tuned to 50 Elo pts — a large, real effect), tuned K/home-adv |
| WNBA | 0.2203 | **0.2145** | same upgrades as NBA |
| FIFA World Cup | 0.2448 | **0.2026** (calibrated) | biggest win: qualifiers added (288 → 1,189 matches), tuned K, Platt calibration |
| ATP | 0.2267 | **0.2180** | moved from ESPN (2024+, keyword-guessed surface) to TML-Database (2019+, 19k matches, REAL surface labels); with true surfaces the surface-blend weight tripled (0.15 → 0.4) and actually earns its keep |
| WTA | 0.2283 | **0.2278** | same |
| MLB | 0.2477 | **0.2437** | starting-pitcher ratings + margin-of-victory + tuning; still the weakest — baseball is genuinely high-variance, and the tuned model now honestly refuses to predict outside ~28-78% |

The v2 MLB model's calibration table is now clean (each bucket's predicted
average matches its actual rate within ~1 point) — the earlier
overconfidence at the extremes is gone, replaced by an honest admission
that MLB games are rarely more certain than ~70/30. Signals there are
trustworthy but small; the divergence threshold does the work of deciding
whether they're tradeable.

**Esports calibration** (walk-forward Brier, latest tuning): LoL **0.219**
(tuned k=72; well-calibrated, on par with tennis), CS2 ~0.231, Valorant
~0.237, Dota 2 ~0.247 (weakest esport — thinnest history). All are team-level
Elo with the roster-change guard on top; a true *player*-level model is a
future upgrade (see roadmap) and only feasible for LoL and Dota on free data.

**FWC note:** Polymarket structures World Cup markets as one market per
*team* ("Will France win: Yes/No"), not one per game — the opponent is found
via a sibling market sharing the same event prefix (see
`divergence_bot._fwc_opponent_map`). Also, soccer has real draws, so a plain
win/loss Elo isn't enough: `elo/soccer.py` decomposes the Elo expected score
into separate win/draw/loss probabilities using a simple, documented
heuristic (draw likelihood peaks when teams are evenly matched). Ratings are
built from World Cup *finals* matches only (a few hundred games across
recent tournaments) — noisier than MLB/NBA, treat with more skepticism.

## Setup

1. **Python 3.10+**, then from this folder:
   ```
   pip install polymarket-us tzdata
   ```

2. **Edit `config.py`**: paste your KEY_ID and SECRET_KEY (from
   polymarket.us/developer — these can be the same credentials as the other
   bot, or separate ones, your choice). Never share these.

3. **ITF tennis data (optional, manual step)**: ATP and WTA are built
   automatically from ESPN — nothing to do for those. ITF (the lower tier
   below ATP/WTA, Polymarket codes ITFME/ITFWO) isn't covered by ESPN, and
   the conventional free alternative (Jeff Sackmann's `tennis_itf` GitHub
   repo) returned 404 when this project was built (2026-07-07) — it appears
   to have moved or gone private. If you want ITF coverage too:
   - Search for a current mirror of Sackmann's ITF match-data CSVs (search
     GitHub for `tennis_itf`, or check `jeffsackmann.com` for where the data
     lives now).
   - Any CSV with `tourney_date`, `winner_name`, `loser_name` columns works,
     regardless of source. Place them in `data/tennis/itf/*.csv`.
   - If you skip this, ITF markets are simply skipped (no model) rather than
     traded blind — ATP/WTA already cover a solid chunk of tennis volume on
     their own.

4. **Build Elo ratings:**
   ```
   python build_ratings.py
   ```
   Logs games processed and rating spread per sport — a sport that comes
   back with 0 games or a suspiciously flat rating spread means the
   ingestion silently failed for that sport; check the log before trusting it.
   Historical results are cached locally (`data/cache/`), so rebuilds only
   refetch recent, still-changing date ranges.

5. **Sanity-check the model before trusting it with money:**
   ```
   python backtest.py
   ```
   Prints Brier score, log-loss, and a calibration table per sport (walk-
   forward: each game is predicted using only ratings built from *earlier*
   games, same information the live bot would have had). A sport whose
   calibration table is way off (e.g. "70-80% predicted" games only winning
   40% of the time) needs the adapter revisited before it trades.

5b. **(Occasional) re-tune hyperparameters:**
   ```
   python tune.py
   ```
   Grid-searches each sport's model constants (K-factor, home advantage,
   pitcher weight, rest penalty, surface blend, draw rate) against the
   walk-forward Brier score, fits a Platt-scaling probability calibration
   where it demonstrably helps on held-out data, and writes winners to
   `model_params.json` — which every other command picks up automatically.
   Already run once (see the results table above); worth re-running after a
   new season's worth of data, not daily.

6. **Eyeball live candidates:**
   ```
   python divergence_bot.py discover
   ```
   Prints current markets in supported sports with computed
   model_prob/market_price/divergence side by side.

7. **Run (dry-run by default):**
   ```
   python divergence_bot.py
   ```
   Logs to `divergence_bot.log`, records simulated positions in
   `positions.db`. Check any time with:
   ```
   python divergence_bot.py status
   ```

## Reporting

Same reporting stack as the original bot, adapted for this strategy:

- **`python divergence_bot.py status`** — Today / This Week / This Month /
  Overall: P&L, record, win rate, entries, open count, plus the average
  divergence at entry per period. A bet belongs to the period it was
  *placed* in; its result counts there whenever it settles, so a period's
  Entries and Record always describe the same cohort of bets.
- **Edge-validation table** (in `status` and the daily Discord digest) —
  settled results grouped by divergence size at entry (5–10%, 10–15%,
  15–20%): record, win rate, what the model predicted on average, and P&L.
  This is THE question the strategy has to answer — if bigger
  model-vs-market gaps don't win more, the edge isn't real. Judge nothing
  before ~100 settled positions.
- **Discord status embeds** (`DISCORD_WEBHOOK_URL` in config.py) — posted
  on start, on every settlement, and every `DISCORD_STATUS_INTERVAL_MIN`
  minutes. Same layout/colors as the original bot, username "Divergence
  Bot" so the two are distinguishable if they share a channel.
- **Per-settlement messages** (`DISCORD_SETTLEMENT_WEBHOOK_URL`) — one
  polished embed per settled position, including the strategy-specific
  line: *model 79% vs market 61% (edge +17.8%)* — so every result can be
  judged as a forecast, not just a win or a loss.
- **Daily digest** (once per day at/after 22:00 reporting time) —
  all-time per-sport record sorted worst win rate first, plus the
  divergence-bucket edge table.

Webhooks are blank by default. You can paste the same webhook URLs used in
the other bot's config.py (messages then share a channel, distinguished by
username) or create a fresh Discord channel + webhook for this bot.

## Keeping data fresh (24/7 operation)

Data updates in three layers — for a server bot, **all three are now
automatic**; you just run `python divergence_bot.py` and leave it:

1. **Live in-game data** (injuries, probable pitchers, esports rosters,
   tennis surface) is fetched *during* each scan cycle on its own short TTL
   (30 min – 12 h), so it's always current on game day. Nothing to schedule.
2. **Historical results / Elo ratings** are rebuilt by the running bot every
   `DATA_REFRESH_HOURS` (default 6 → **4×/day**). The bot spawns
   `refresh_data.py` as a *background process* (trading never pauses) and
   **hot-reloads** the freshly-built ratings when it finishes — no restart.
   A rebuild that hangs or crashes can't take the bot down (separate
   process; last-good ratings stay in use). See `refresh.py`.
3. Because that refresh runs the esports fetchers each time, the esports
   stores also **deepen and stay current automatically** (they accumulate
   incrementally, resilient to a flaky source across cycles).

Manual/one-off refresh (or if you prefer external cron/Task Scheduler
instead of the built-in loop): `python refresh_data.py` does the same work
once and exits. A single sport: `python build_ratings.py mlb` (merges into
the existing file rather than wiping other sports).

**Occasional (not automated):** re-run `python tune.py` after a season's
worth of new data to re-fit hyperparameters, and re-derive per-sport
divergence thresholds from the fresh noise floors. These change slowly and
are deliberately left as a manual, reviewed step.

## Injuries, lineups, and why the bot trades late

The most dangerous failure mode for a divergence strategy is **adverse
selection**: the market reprices on news (star scratched, lineup change)
within minutes, while an Elo model only updates on final scores. A stale
rating then shows a fat "divergence" that is really the market being
right and the model being blind. Three defenses, layered:

1. **Late entry window** — `TRADE_BEFORE_START_MINUTES = 60`: the bot only
   enters during the final hour before start, after injury reports and
   starting lineups are public and priced in. (Liquidity is also best then.)
2. **Key-player injury filter (NBA/WNBA)** — before trading, the bot checks
   ESPN's live injury report against each team's top-3 scorers by PPG
   (`elo/injuries.py`). If any is listed Out/Doubtful, the market is
   skipped with a logged reason — the rating was earned by a roster that
   isn't playing tonight. Verified live on day one: it correctly skipped
   Fever games (Caitlin Clark out) and Aces games (A'ja Wilson out) — and
   retroactively would have skipped the bot's very first "candidate"
   (Chicago Sky market, Sky's #2 scorer out — that +17.8% "edge" was
   partly the market pricing an absence the model couldn't see).
   Day-To-Day/Questionable players usually play, so those don't trigger.
3. **MAX_DIVERGENCE guard (all sports)** — an edge bigger than 20 points is
   treated as "the market knows something we don't" and skipped. Real
   mispricings on liquid markets are small; the too-good ones are traps.

Per-sport injury coverage: MLB's one dominant player (the probable starter)
is tracked directly and a late scratch flows in when the 30-minute pitcher
cache refreshes; tennis needs no filter (if the match starts, both are
playing); FWC/soccer has no free lineup feed — the late window and the
divergence cap are the mitigation there.

## Name matching — the highest-maintenance part of this system

Polymarket's team/player name strings won't always exactly match the Elo
source's names. `name_match.py` tries an exact match, then a small seed
alias table, then fuzzy matching as a last resort — logging every fuzzy
match and every outright miss. A market with an unresolved name is skipped,
never guessed. Check `divergence_bot.log` periodically for
`no Elo match for ...` warnings and add real fixes to `name_match.ALIASES`.

## Hard limits (enforced in code, config.py)

| Rule | Default |
|---|---|
| Stake | quarter-Kelly sized by model edge (KELLY_FRACTION=0.25), clamped to $1–$10; set KELLY_FRACTION=0 for flat 1%-of-capital sizing |
| Max open positions | 15 |
| Max entries per sport per day | 5 |
| Daily loss halt | 5% of day-open balance |
| Divergence threshold | per sport, derived from each model's measured noise floor: ATP 4.5%, WTA/NBA 5%, WNBA/MLB 6.5%, FWC 8.5% — and 20% maximum (bigger = market knows something) |
| Price guard rails | only trade in the 5%–95% price range |
| Entry window | final 60 minutes before start only (injury/lineup news is public by then) |
| Key-player filter | NBA/WNBA markets skipped if a top-3 scorer is Out/Doubtful |
| Pregame only | live/in-play games are out of scope — an Elo model has no opinion on in-game state |

These are deliberately more conservative than the existing bot.py's — this
strategy has never traded before. Loosen once `status` and `backtest.py`
give you real evidence to loosen them with.

## Running 24/7 on a server

The bot is self-contained: one long-running process that trades, refreshes
all data 4×/day, and hot-reloads its own ratings (see "Keeping data fresh").
For a server, just keep that process alive and auto-restarting:

- **Windows:** run under NSSM (`nssm install DivergenceBot python
  D:\polybot\divergence-bot\divergence_bot.py`) or a Task Scheduler task set
  to "restart on failure" and "run whether logged on or not".
- **Linux:** a systemd unit with `Restart=always` running
  `python divergence_bot.py` in this directory.

That's all that's needed — no separate cron for data, because the bot
refreshes itself. (If you *prefer* external scheduling, set
`DATA_REFRESH_HOURS` very high to idle the built-in refresh and run
`python refresh_data.py` from cron/Task Scheduler every 6 h instead.) Logs:
`divergence_bot.log` (rotating) and `refresh_data.log` (last refresh).

## Going live

Same posture as the existing bot: judge nothing before ~100 settled
positions AND a calibration table from `backtest.py` that looks reasonable.
When ready, flip `LIVE = True` in `config.py`. With the self-refresh loop
you no longer need to restart for fresh ratings — but flipping LIVE does
require a restart (it's read at startup).

## Known limitations / good next steps

- NPB/KBO baseball still have no model and are excluded — needs research
  into a free API covering those leagues.
- ITF tennis needs the manual CSV step above before it does anything (ATP/WTA
  work out of the box).
- Esports team-name reconciliation will need ongoing alias curation — e.g.
  OpenDota calls BetBoom "BB Team" (aliased); every unmatched name is
  logged, and those markets are skipped rather than guessed.
- MLB remains the weakest sport, and its ceiling looks structural.
  **Tried (2026-07-09):** seeding pitcher ratings from real prior-season ERA
  (bulk stats endpoint, walk-forward honest — prior season only, rookies
  stay neutral). Kept (tuned seed scale 70, fixes the cold-start on
  principle) but the honest verdict is near-null: Brier 0.2437 → 0.2436.
  Every pitcher-related lever is now tuned; further MLB gains likely need
  non-Elo features (park factors, bullpen usage, weather), not more Elo.
- **LoL player-level Elo — VALIDATED and wired (2026-07-09).** Rates
  individual players (not team names); a team's strength for a match is the
  mean rating of the five who actually play. Head-to-head on identical games
  (`compare_lol_models.py`), player-Elo **beat team-Elo: Brier 0.2253 vs
  0.2300** (k=48). Data is Oracle's Elixir (`fetch_oe.py` — bulk per-game
  player CSVs; Google Drive quota-blocks recent years some days, so it also
  pulls a GitHub mirror and is idempotent/resumable). `build_ratings.py lol`
  writes a `lol_player_model.json` sidecar (player ratings + each team's
  latest lineup as the live "who plays" proxy). It **auto-activates for LoL
  in the live bot once the data is fresher than `LOL_PLAYER_FRESHNESS_DAYS`**
  (config) — until then, or if a lineup can't be resolved, it falls back to
  the current-data team-Elo. Right now only 2023 OE data is obtainable
  (2024/25 Drive-quota-blocked), so the freshness gate keeps team-Elo live;
  rerun `python fetch_oe.py` (quota resets ~daily) then `build_ratings.py
  lol`, and player-Elo takes over automatically. **To finish going live:**
  land 2024/2025 CSVs via fetch_oe.py.
- **Dota player-Elo** is groundwork-laid: per-match rosters accumulate at
  ~80/day (`esports.capture_dota_rosters`, wired into build_ratings), so the
  same OE-style comparison becomes possible there in a few months. CS2 and
  Valorant have no free per-match roster history — they'd need GRID Open
  Access (free but application-gated) or a paid feed.
- WTA has no live TML-style mirror (Sackmann's repos 404 as of 2026-07);
  it stays on ESPN 2024+. ATP moved to TML-Database and improved to 0.218.
- The back-to-back rest penalty relies on ratings being rebuilt daily
  (build_ratings.py) so each team's last-played date stays current.
- A natural "real ML" upgrade path: gradient boosting on engineered
  features with these Elo ratings as inputs — the data cache and
  walk-forward harness here are exactly the foundation that needs.
