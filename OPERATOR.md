# OPERATOR.md — the single source of truth for this project

**If you are an AI assistant (Claude, Codex, or anything else) starting a new
session on this repo: read this whole file before touching anything.** It is
the only doc. There is no README, no RUNBOOK, no separate TODO board. If
something here is wrong, fix *this file* in the same change.

**If you are the operator (human): this is your runbook.** Sections you use
day to day: [Operating cadence](#operating-cadence--what-to-run-and-when),
[Diagnostics](#diagnostics--run-these-when-something-seems-off),
[Emergencies](#emergencies).

---

## 1. What this is, in one paragraph

A Polymarket US trading bot that builds its own Elo win-probability per
matchup from historical results, compares it to the market's price, and enters
only when the two disagree by more than a per-sport threshold — on whichever
side (favorite or underdog) is underpriced. It is **not** a price-band bot.
The entire thesis is: *our probability is better than the market's, and the
gap is tradeable after fees.* Everything else in this repo exists to protect
that thesis from being wrong (guards) or to measure whether it's actually true
(CLV).

**IT IS LIVE. IT TRADES REAL MONEY.** `config.LIVE = True`, ~$700 bankroll, on
a DigitalOcean droplet. Every change you make can lose money.

---

## 2. Non-negotiable rules

1. **Data flows DOWN from the server, never up.** The server is the source of
   truth for everything it generates:

   ```
   server  --->  local     ALWAYS   (python pull_server_state.py, daily)
   local   --->  server    NEVER    (positions.db, elo_ratings.json,
                                     elo_freshness.json, lol_player_model.json)
   ```

   **NEVER copy any `positions.db` TO the server.** The server's copy is the
   live money record and it never self-heals. Overwriting it destroys real
   trading history that exists nowhere else. Downloading it is not just fine,
   it's the daily job — it's the only backup that isn't on the droplet.

   Only *code* flows up (local → branch → main → scp). Data only comes down.
2. **NEVER `scp -r` or `scp *` toward the server.** Always an explicit list of
   files.
3. **Never scp generated data** (`elo_ratings.json`, `elo_freshness.json`,
   `data/`). The server rebuilds these every 6h and its copies are *fresher
   than yours*. (Unlike `positions.db`, these do self-heal on the next
   refresh if clobbered — but don't.)
4. **Never hand-edit code on the server.** Changes flow
   `local → branch → verify → main → deploy → push`. A server hand-edit is
   silently destroyed by the next deploy, and makes main a lie.
5. **Every change gets its own branch off main.** Never commit directly to
   main. See [the working agreement](#3-working-agreement-how-changes-get-made).
6. **One strategy change per week**, decided from the edge report. Changing
   two knobs at once means learning nothing from either.
7. **When anything looks wrong, paste it to the assistant** — Discord alert,
   digest, audit output, weird numbers. Don't sit on it.

---

## 3. Working agreement (how changes get made)

This is mandatory for every code change, no matter how small. It exists
because this bot handles real money and a "one-line fix" already caused a
real-money incident (see [History](#11-history-incidents-and-what-they-taught)).

1. **Branch off main.** `git checkout main && git checkout -b <short-name>`.
2. **Make the change.** Match the surrounding code's style and idiom.
3. **Verify it actually works** — not "it typechecks", not "tests pass":
   *drive the affected path and observe the behavior.* For this codebase that
   usually means running the real function against the real data (e.g. load
   the model and predict; run the audit; replay a settlement). If you cannot
   observe it, say so out loud rather than claiming it works.
4. **Hunt edge cases, then fix them.** Ask specifically: what happens on an
   empty result, a network failure, a partial fill, a `None` where a tuple was
   expected, a duplicate, a restart mid-operation, a stale cache? The two
   worst bugs this project has had were both edge cases in exactly this list
   (a paginated listing, and a `None` that meant two different things).
5. **Report honestly**, in two explicit lists: **what I did** and **what I did
   NOT do / did not verify / left as a known gap.** The second list is the
   important one. Never let a gap hide in silence.
6. **Only then merge to main** — with the operator's go-ahead, never
   unilaterally: `git checkout main && git merge --no-ff <branch>`.
7. **Deploy to the server, and confirm main == server.** Main and the server
   must never disagree. Run the [parity check](#parity-check-main-vs-server)
   after every deploy.
8. **Push to GitHub.** `git push origin main` — only once the change is
   verified, merged, deployed, and parity is clean. Nothing is "done" until
   it's pushed: an unpushed commit lives on one Windows machine and is one disk
   failure from gone. The repo is **private**
   (`github.com/rynn-arriola/Polymarket-Bot`).

   > **Before the first push of anything new, check for secrets.** Real keys,
   > tokens, and webhook URLs live in `credentials.py`, which is **gitignored**.
   > `config.py` IS tracked but holds only `_secret("NAME")` lookups — never a
   > literal value. Keep it that way: a secret pushed to GitHub is not undone by
   > deleting it, it's undone by rotating the key.

**The finish line for any change is: verified → merged → deployed → parity
clean → pushed.** Stopping short of that leaves the four copies of this project
(working tree, main, server, GitHub) disagreeing, and the whole point of the
workflow is that they never do.

---

## 4. The server

| | |
|---|---|
| Host | `root@142.93.58.166` (DigitalOcean, Ubuntu 24.04, **961 MB RAM**, 24 GB disk) |
| Code | `/root/divergence-bot` |
| Bot process | one long-running `python3 divergence_bot.py` inside tmux session `bot` |
| Auth | **SSH key only — password auth is OFF.** Key: `~/.ssh/polybot_droplet` |
| Python | **`/usr/bin/python3.12`, system-wide.** Install deps with `pip3 install --break-system-packages`. The `venv/` directory in `/root/divergence-bot` is a **decoy** — nothing uses it. Installing into it silently fixes nothing. |
| Local dev copy | `d:\polybot\divergence-bot` (Windows, PowerShell) |

Assistant connects with:
```bash
ssh -i ~/.ssh/polybot_droplet -o IdentitiesOnly=yes root@142.93.58.166 '<cmd>'
scp -i ~/.ssh/polybot_droplet -o IdentitiesOnly=yes <file> root@142.93.58.166:/root/divergence-bot/
```
To revoke that access: delete the `claude-code@polybot-droplet` line from
`/root/.ssh/authorized_keys`. That is the entire undo.

### Deploying

```powershell
cd d:\polybot\divergence-bot
scp <exact files, listed explicitly> root@142.93.58.166:/root/divergence-bot/
# elo/ files MUST go to .../divergence-bot/elo/ — not the top level.
```

> A past mis-targeted scp left stale copies of `esports.py`, `history.py`, and
> `rosters.py` at the server's top level. They are dead (Python imports
> `elo/*`, not these) but they are a trap for the next reader. Get the
> destination right.

**Restart is required for:** `divergence_bot.py`, `config.py`,
`credentials.py`, `risk.py`, `db.py`, `reporting.py`, `name_match.py`,
`xgb_live.py`, and any `elo/*` change you want live immediately.
**Not required for:** `fetch_oe.py`, `refresh_data.py`, `build_ratings.py`
(they run as fresh subprocesses every refresh).

```bash
tmux attach -t bot          # Ctrl+C to stop
python3 divergence_bot.py   # relaunch
# Ctrl+B then D to detach
```
Non-interactively (what the assistant uses):
```bash
# Count ONLY real bot processes. Do NOT use `pgrep -f divergence_bot.py`:
# -f matches the whole command line, so it also matches the very shell running
# this snippet (which contains the string), and reports a phantom extra bot.
count() { ps -eo cmd | grep -c "^python3 divergence_bot\.py$" || true; }

tmux send-keys -t bot C-c
for i in $(seq 1 15); do [ "$(count)" = "0" ] && break; sleep 1; done
[ "$(count)" = "0" ] || { echo "old process still alive - do NOT relaunch"; exit 1; }
tmux send-keys -t bot "python3 divergence_bot.py" Enter
sleep 25
[ "$(count)" = "1" ] || echo "WRONG PROCESS COUNT - investigate before walking away"
```
> **Relaunching before the old process exits gives you TWO live bots placing
> duplicate orders.** SIGINT cleanup takes a few seconds. Confirm the count is
> 0 before relaunching and 1 afterwards — with the `count()` above, not
> `pgrep -f`, which lies. (A guard using `pgrep -f` once reported a phantom
> second bot, refused to relaunch, and left the bot down for ~100 s.)

After any deploy: watch the log for the startup banner and one clean
`Cycle done`, then run the parity check.

### Parity check (main vs server)

**Main and the server must never disagree.** Run this after every deploy, and
in the weekly review:

```bash
python check_parity.py     # from the local repo; exit 0 = in sync, 1 = drift
```

It compares every tracked runtime file against the server and reports:

| | meaning |
|---|---|
| `DIFFERS` | **the dangerous one** — the server is running code that isn't on main, or main has a fix that was never deployed |
| `MISSING` | tracked in git, never deployed |
| `EXTRA` | a `.py` on the server that git doesn't know about — usually a stale copy from a mis-targeted `scp` |

Line endings are normalized (the repo is CRLF on Windows, the server is LF), so
it only reports *real* content drift. Generated data (`elo_ratings.json`,
`elo_freshness.json`, `lol_player_model.json`) is deliberately **not** compared
— the server rebuilds those every 6h and its copies are *supposed* to be
fresher. `credentials.example.py` is expected to be absent on the server.

---

## 5. Operating cadence — what to run and when

### DAILY (~2 min)

**1. Pull the server's state down and audit reporting.**
```bash
python pull_server_state.py    # local, from the repo
```
Pulls the server's live state **down** to `server_mirror/`: the live
`positions.db` and the ratings files the server rebuilds every 6h. Two reasons
this is a daily habit and not an occasional one:

- **Local goes stale fast.** The server rebuilds ratings 4×/day. Analysis run
  against local ratings that are two days old is analysis of a bot that doesn't
  exist.
- **`positions.db` exists in exactly ONE place: the droplet.** If it dies, the
  entire real-money trading history dies with it. This pull is the only copy
  that isn't on the droplet. It keeps 30 dated snapshots.

It is **one-way by construction**: it takes a read-only `sqlite3 .backup`
snapshot on the server (safe while the bot is writing — the DB is in WAL mode,
so a plain `scp` of the file could catch a torn write) and copies *down*. It
never writes to `/root/divergence-bot`. A corrupt or interrupted download is
verified and rejected *before* it can replace the last good backup.

After the pull, it runs `audit_reporting.py` on the server against the live DB
and exchange API. Expect `CLEAN — reporting inputs are consistent`. If the
audit flags anything or the command fails, the backup is still valid; paste
the full audit output before making changes.

Your local `positions.db` is the **dry-run/dev** database (`live=0`) and is
**never touched** — the live DB lands in `server_mirror/`, kept separate so the
two can never be confused. `server_mirror/` is gitignored: it holds real
trading history.

**2. Glance at Discord.** Nothing to run.

| Channel | What good looks like | When to act |
|---|---|---|
| Status (every 5 min) | P&L/record ticking along | — |
| Settlements | a card per finished bet | a bet you KNOW settled has no card → paste to assistant |
| CLV report (4×/day) | 🟢 market moves toward our bets | persistent 🔴 across days → raise it |
| Paper CLV report | the 5–95% ledger, incl. signals the 30% live floor rejected | this is the evidence for moving the live price floor |
| Errors channel | quiet; 6pm digest small or green | any 🚨 CRITICAL → paste to assistant *same day* |

### WEEKLY — Sunday, ~5 min

```bash
ssh -i ~/.ssh/polybot_droplet root@142.93.58.166
cd /root/divergence-bot
python3 edge_report.py        # THE strategy review — paste the ENTIRE output
python3 audit_reporting.py    # are the books true? expect "CLEAN"
python3 suggest_aliases.py    # unmatched names worth aliasing
```
And locally: `python check_parity.py` — expect `IN SYNC`.
Paste all three outputs. Rules: **at most ONE config/strategy change per
week**, and judge nothing on fewer than ~100 settled positions. If the edge
report and your gut disagree, believe the edge report.

### MONTHLY — ~20 min

```bash
python3 backtest.py           # walk-forward Brier + calibration, per sport
python3 tune.py               # re-fit K/home-adv/etc → model_params.json
```
Then, off the back of those:
- **Re-derive divergence thresholds** from the fresh per-sport noise floors.
  A threshold below its sport's noise floor + fees is just paying rake.
- **Check calibration tables.** A sport whose "70–80% predicted" bucket wins
  40% of the time is broken; pull it before it bleeds.
- **Review the parity check** and the [decision board](#10-decision-board--what-is-actually-open).
- `tune.py` and `backtest.py` are read-only w.r.t. money — safe to run any time.

### QUARTERLY / AS NEEDED

- Re-examine the [dead ends](#9-dead-ends--do-not-re-run-these) list. Only
  re-open one if **new data columns** landed — never to re-run the same
  experiment on the same features.
- Prune disk: `pip3 uninstall nvidia-nccl-cu12` (303 MB of GPU libraries
  xgboost drags in; useless on this CPU-only droplet — harmless, just waste).

---

## 6. What's automatic (never action these)

- Market scan, entries, fill confirmation, cancels: every **60 s**.
- The market **catalog** (the ~9,000-market list) refetches every
  **10 min** (`MARKET_LIST_REFRESH_MIN`), paced, and is reused in between —
  it's discovery metadata only; **entry prices always come from a fresh
  per-market bbo call every cycle.** This is the rate-limit fix; see
  [History](#11-history-incidents-and-what-they-taught).
- Cloudflare-ban circuit breaker: if the exchange answers with a ban page
  (error 1015), all exchange calls pause `API_BAN_COOLDOWN_MIN = 5` min so
  the ban can expire, instead of hammering through and extending it. Expect
  the log line "Cloudflare ban cooldown"; positions are safe while paused.
- Reconcile give-up: a settled row whose resolution activity still hasn't
  appeared `RECONCILE_GIVE_UP_DAYS = 3` days after settlement keeps its
  estimated P&L as final (the exchange never posts one for some markets —
  otherwise the row polls the feed every cycle forever).
- Unfilled orders cancelled after **10 min** (`CANCEL_UNFILLED_AFTER_MIN`).
  `confirm_fills` **never** cancels on absence — giving up is solely
  `cancel_stale_orders`' job. This is load-bearing; see
  [History](#11-history-incidents-and-what-they-taught).
- Settlement detection + Discord card: within ~1 min of exchange resolution.
- P&L reconciliation to exchange-exact figures: the exchange *restates* cost
  basis (rolls fees in) minutes after posting, so P&L is only stamped final
  after `RESOLUTION_STABLE_MINUTES = 45`. Provisional before that.
- Auto-heal of mis-cancelled rows: every live `cancelled` row is cross-checked
  against the exchange once; still-held → reopened, resolved → settled,
  genuine → flagged `cancel_verified` and never re-checked.
- Ratings/data refresh: every **6 h**, as a background subprocess, hot-reloaded
  without a restart. Trading never pauses; a hung rebuild can't take the bot
  down (last-good ratings stay live).
- Ratings freshness guard: a sport whose ratings haven't rebuilt in
  `RATINGS_STALE_HOURS = 24` is **skipped**, not traded on stale numbers.
- CLV capture: final minutes before every game start.
- Paper/shadow ledger: every candidate clearing the model + quality guards is
  recorded *before* price/risk/order decisions — including ones the 30% live
  floor, loss brake, or position caps rejected. It never touches real P&L.
- Rescheduled matches: kept open (avoids exit fees), start time updated for
  CLV, one extra position slot granted so normal entries aren't blocked.
- Daily loss brake, roster / injury / dormancy guards, price and spread gates.

---

## 7. The money rules (all enforced in `config.py`)

Read these from `config.py` — if this table and the code disagree, **the code
is right and this table is a bug.**

| Rule | Value | Why |
|---|---|---|
| Sizing | flat **1% of day-open bankroll** (`STAKE_PCT`); Kelly is **OFF** (`KELLY_FRACTION = 0`) | |
| Hard stake cap | **2%** of bankroll (`MAX_STAKE_PCT`), min $1 | |
| Bankroll | day-open balance, captured at start of day and held fixed intraday | intraday P&L can't compound sizing |
| Max open positions | **10** | |
| Max entries per sport per day | **5** | correlation cap |
| Daily loss halt | **6%** of day-open balance | new entries stop; open positions still settle |
| Fee buffer | 0.5% of bankroll held back | fees can't push an order over balance |
| Divergence threshold | per sport: ATP 4.5%, WTA/NBA/**LoL** 5%, CS2 6%, ITF 6%, WNBA/MLB 6.5%, Dota2 7%, Valorant 8%, FWC 8.5% | each derived from that model's measured noise floor + fees + margin |
| Max divergence | **20%** | a bigger "edge" means the market knows something we don't — it's a trap, not a gift |
| Price band | valid signals 5–95%; **live orders 30–95%** | below 30% is paper-only (the ledger is gathering the evidence to move this) |
| Entry window | final **60 min** before start, pregame only | injuries/lineups are public and priced by then |
| Key-player filter | NBA/WNBA skipped if a top-3 scorer is Out/Doubtful | |
| Excluded | NBA Summer League (`nbasl`) | same team names, entirely different rosters |

**Why the late entry window exists (the core defense).** The deadliest failure
mode for a divergence strategy is **adverse selection**: the market reprices on
news (star scratched, roster swap) in minutes, while an Elo model only updates
on final scores. The stale rating then shows a fat "divergence" that is really
*the market being right and the model being blind*. Late entry, the injury
filter, the roster guard, and the 20% divergence cap are four layers of defense
against exactly this. Do not weaken them to get more volume.

---

## 8. The models

| Sport | Data source | Walk-forward Brier | Notes |
|---|---|---|---|
| NBA | ESPN | **0.2094** | well-calibrated; MoV scaling + b2b rest penalty |
| WNBA | ESPN | **0.2145** | same upgrades |
| FWC (World Cup) | ESPN | **0.2026** | draw-decomposed; thin sample, treat with skepticism |
| ATP | TML-Database | **0.2180** | real surface labels; surface blend earns its keep |
| WTA | ESPN | **0.2278** | no TML-style mirror exists for WTA |
| MLB | MLB Stats API | **0.2437** | weakest; ceiling looks structural (see dead ends) |
| LoL | Leaguepedia + Oracle's Elixir | **0.2102** (XGB blend) | **the one active XGBoost model** — see below |
| Dota 2 | PandaScore | **0.2146** | 34.8k matches; + dormancy regression |
| CS2 | PandaScore | **0.2250** | 51.9k matches; + dormancy regression |
| Valorant | PandaScore | **0.231** | |
| ITF | *none* | — | no free data source exists; markets are skipped, never guessed |

Lower Brier is better; **0.25 = a coin flip.**

**Elo is the baseline everywhere. XGBoost is a gated override, not a
replacement.** `xgb_live.py` will let a per-sport XGBoost model take over a
sport's probability **only** if a trained model exists that (a) beat Elo
out-of-sample (`beats_elo` in its meta) and (b) was trained within
`XGB_STALE_DAYS = 45`. Otherwise `predict()` returns `None` and the caller
falls back to Elo. Missing model, stale model, failed load, missing dependency
— all fail safe to Elo.

**LoL is the only sport currently on XGBoost** (activated 2026-07-14). It's a
team-Elo + player-aggregate blend built from the Oracle's Elixir sidecar, test
Brier 0.2102 vs 0.2183 for Elo. Its entire feature row comes from the OE
sidecar — **never** the live Leaguepedia match engine, which is a different
rating space; mixing them is train/serve drift. Confirm it's actually live with:
```bash
grep -i "XGB lol" divergence_bot.log | tail -1   # want: "XGB lol ACTIVE"
```

> **The trap that already bit us:** a model can clear every gate and still be
> silently inert if its runtime dependency isn't installed on the server. The
> gate passes, `import xgboost` raises, the broad `except` swallows it, and the
> sport quietly trades on Elo. **When activating any XGB sport, verify
> `requirements.txt` ships its deps AND that they're installed on the server's
> system python.**

### Name matching — the highest-maintenance part of the system

Polymarket's name strings don't always match the Elo source's. `name_match.py`
tries exact → alias table → fuzzy, logging every fuzzy hit and every miss.
**An unresolved name is skipped, never guessed.** Run `suggest_aliases.py`
weekly and add real fixes to `name_match.ALIASES`. Most residual misses are
lower-tier ITF/Challenger tennis players who genuinely aren't in any free
dataset — those are unfixable, not bugs.

### Roster changes — the esports blind spot

An Elo rating follows the team *name*; a rebuilt roster inherits the old tag's
rating. `elo/rosters.py` snapshots lineups and **skips** a team's markets when
2+ players change, until it has played `ROSTER_REACCEPT_MATCHES = 15` games
under the new lineup. The guard is **fail-open** (an unfetchable roster never
blocks trading — the endpoints are flaky and failing closed would halt most
esports trading). Roster sources: PandaScore for Valorant/Dota2/CS2,
Leaguepedia for LoL.

---

## 8b. Bootstrapping a fresh copy (rarely needed)

```bash
pip install -r requirements.txt        # incl. xgboost + numpy for the LoL model
# put KEY_ID / SECRET_KEY in config.py (polymarket.us/developer). Never share.
python build_ratings.py                # build Elo from cached history
python backtest.py                     # sanity-check calibration BEFORE trusting it
python divergence_bot.py discover      # eyeball live candidates
python divergence_bot.py               # run (LIVE is read from config at startup)
```
`build_ratings.py` logs games processed and rating spread per sport. **A sport
that returns 0 games or a suspiciously flat spread means ingestion silently
failed** — check before trusting it. Rebuilds only refetch recent date ranges
(`data/cache/`). One sport at a time: `python build_ratings.py mlb` (merges,
doesn't wipe the others).

**ITF tennis (optional, manual).** ATP/WTA build automatically; ITF has no free
source (Sackmann's `tennis_itf` repo 404s as of 2026-07). To enable it, find a
current mirror of his ITF CSVs — **any CSV with `tourney_date`, `winner_name`,
`loser_name` columns works** — and drop them in `data/tennis/itf/*.csv`. Skip
this and ITF markets are simply skipped, never traded blind.

---

## 9. Dead ends — do NOT re-run these

Re-running a settled experiment burns days and teaches nothing. Each of these
was tested properly (walk-forward, multi-seed) and **lost**. Only re-open one
if *genuinely new feature columns* land — never on the same features.

- **XGBoost on Elo-derived features alone: DEAD.** Only ties Elo on
  NBA/ATP/WTA. The algorithm was never the bottleneck — **data is**. This is
  why effort goes into new data sources and measurement, not new models.
- **Recency weighting: DEAD, all 10 sports.** Staleness is already priced into
  `elo_exp`.
- **Esports context features** (bo-format, tier, fatigue): **DEAD.**
- **MLB pitcher-rating seeding from prior-season ERA:** near-null (0.2437 →
  0.2436). Every Elo-shaped MLB lever is now tuned. Further MLB gains need
  non-Elo features (park factors, bullpen usage, weather) — or nothing.

The **one** thing that cleared the bar was the LoL player blend, and it needed
a genuinely new data source (per-game player data) to do it. That's the
pattern: **new data wins, new algorithms don't.**

---

## 10. Decision board — what is actually open

- **CLV is the judge.** Closing-line value is the true-north metric: does the
  market move *toward* our bets after we place them? It decides whether the
  Elo edge is real before any further investment. Early signal was promising
  (15–21% divergence bucket, ~71% win rate) but the sample is small. **Let it
  accumulate. Do not add strategy complexity while this is unresolved.**
- **Per-sport CLV auto-guard** (auto-pause sports that don't beat the close) is
  the natural next build — but only *once there's enough data to act on*.
- **The live 30% price floor** is under review. The paper ledger records the
  full 5–95% range specifically so its price-band table can answer whether the
  floor is costing us money. That table is the evidence; wait for it.
- **LoL XGB blend** just went live (2026-07-14). Watch its CLV and win rate:
  a +0.008 Brier improvement in backtest is a *hypothesis* about real money,
  not a fact about it.

---

## 11. History: incidents and what they taught

Read these before you "simplify" the order-handling code. Both were subtle,
both cost real money, and both look like over-engineering until you know why.

- **Mis-cancel #1 — pagination (2026-07-13).** Fill/cancel checks listed the
  *whole* portfolio to see if a position was held. That listing is
  **paginated**: with enough open positions, held markets past page 1 looked
  *absent*, and absent-after-cancel was read as cancelled. Three filled orders
  were marked `cancelled` (~$31 of invisible losses). Fix: every decision path
  now uses a **per-market** query (`_position_quantity`), never a full listing.
- **Mis-cancel #2 — read-after-write lag (2026-07-14).** `confirm_fills` ran in
  the same cycle as order placement, and the exchange's *read* path hadn't
  indexed the new order yet → 404 + empty portfolio → "definitively gone".
  Orders were cancelled <1 s after being placed. Fix: **`confirm_fills` never
  cancels on absence.** Giving up belongs solely to `cancel_stale_orders`,
  which waits 10 min and issues a definitive cancel first. *Don't "optimize"
  this back.*
- **The `None` that meant two things (2026-07-14).** The auto-heal's resolution
  check returned `None` for both "the feed says there's no resolution" and "the
  check failed (exception / rate limit)". A single transient failure therefore
  marked a row permanently verified. Fix: an explicit `RESOLUTION_CHECK_FAILED`
  sentinel. **Never let one sentinel value mean both "answered no" and
  "couldn't answer".**
- **Cost-basis restatement (2026-07-13).** The exchange rolls fees into a
  resolution's cost basis *minutes after* posting it. Reading too early
  overstated 16 settled rows by ~$10.6. Fix: `RESOLUTION_STABLE_MINUTES = 45`.
- **The silently-inert model (2026-07-14).** See the XGB trap in
  [section 8](#8-the-models).
- **The Cloudflare ban (2026-07-16).** api.polymarket.us banned the droplet
  for ~3 h (error 1015, "banned you temporarily"). Cause: the full market
  catalog (~9,000 markets = ~90 paginated calls in a ~6 s burst) was refetched
  **every 60 s cycle**; a busy match window stacked settlement and CLV calls
  on top and tipped it over Cloudflare's burst limit. No money was lost —
  every check failed loudly and concluded nothing (the fail-open design), and
  all ten affected positions settled and reconciled correctly after the ban
  lifted. Fixes: the catalog is now cached and refetched every 10 min with
  paced pagination (prices were never in the catalog — they come from
  per-market bbo calls, still every cycle), and a ban-page answer now pauses
  all exchange calls for 5 min (`api_guard.py`) instead of hammering through.

---

## 12. Diagnostics — run these when something seems off

```bash
cd /root/divergence-bot

python3 audit_reporting.py    # Are the books true? Drift, missed settlements,
                              # stuck rows. Expect: "CLEAN". THE first thing to
                              # run whenever reporting looks wrong.
python3 edge_report.py        # Where is the edge working? (the Sunday report)
python3 divergence_bot.py errors    # recent warnings/errors, no Discord needed
python3 divergence_bot.py status    # P&L / record / open positions
python3 divergence_bot.py discover  # what the model sees in current markets
python3 suggest_aliases.py          # unmatched names worth aliasing
python3 repair_miscancelled.py      # standing tool: restore rows wrongly marked
                                    # cancelled (dry-run default; --apply to act)

# What's open/pending right now
sqlite3 positions.db "SELECT market_slug,status,created_at FROM positions
  WHERE live=1 AND status IN ('open','pending') ORDER BY created_at DESC;"

tmux attach -t bot            # is it alive / what's it doing (Ctrl+B D to leave)
```

Manual (human-entered) bets live in a separate `manual_trades` table and are
never touched by the bot loop:
```bash
python3 manual_trades.py add --slug <slug> --sport CS2 \
  --matchup "A vs B" --side "A" --price 0.42 --quantity 10
python3 manual_trades.py cashout <id> --close-price 0.55
python3 manual_trades.py close <id> --status won|lost
python3 manual_trades.py cancel <id>
python3 manual_trades.py list | report
```

---

## 13. Emergencies

**STOP TRADING NOW (kill switch):** `Ctrl+C` in the tmux session. Open
positions still settle on the exchange by themselves — **stopping the bot never
abandons money.** To run without trading, set `LIVE = False` in `config.py`,
deploy, restart.

**Bot down / no Discord for >15 min:**
```bash
tmux attach -t bot          # crashed? the traceback is right there — copy it
python3 divergence_bot.py   # relaunch
```

**Droplet rebooted (tmux gone):**
```bash
tmux new -s bot
cd /root/divergence-bot && python3 divergence_bot.py
# Ctrl+B D
```

**🚨 "UNTRACKED order/position" alert:** don't panic — the bot already blocked
re-entry. Check the Polymarket app for that market, then paste the alert to the
assistant.

**"DAILY LOSS LIMIT hit":** not an emergency. That's the brake working. New
entries halt until midnight ET; open positions still settle.

**Settlements missing / summary doesn't match Polymarket History:** run
`audit_reporting.py`. If it flags "resolved on exchange but DB status=cancelled",
that's the known mis-cancel class — `repair_miscancelled.py` and the auto-heal
handle it. Paste the output.

---

## 14. Code map

| File | What it is |
|---|---|
| `divergence_bot.py` | the main loop: scan → evaluate → order → confirm → settle. Also `status`, `discover`, `errors` subcommands. |
| `config.py` | **every knob.** The money rules live here. Not in git (secrets); `credentials.example.py` is the template. |
| `risk.py` | sizing, position caps, daily loss brake |
| `db.py` | `positions.db` schema + migrations (SQLite, WAL) |
| `reporting.py` | Discord embeds: status, settlements, daily digest, CLV, paper CLV |
| `audit_reporting.py` | **the standing reporting-truth diagnostic.** Run it whenever numbers look wrong. |
| `edge_report.py` | the weekly strategy review |
| `check_parity.py` | is the server running exactly what's on main? (run local) |
| `pull_server_state.py` | **daily.** Pulls live DB + server ratings DOWN into `server_mirror/`. One-way; the only backup of the live money record. |
| `repair_miscancelled.py` | standing repair tool for wrongly-cancelled rows |
| `name_match.py` | Polymarket name → Elo name resolution (exact → alias → fuzzy) |
| `api_guard.py` | Cloudflare-ban detection + cooldown (the rate-limit circuit breaker) |
| `elo/` | the rating engines — `engine`, `basketball`, `tennis`, `mlb`, `soccer`, `esports`, `lol_players`, `rosters`, `injuries`, `history`, `params` |
| `xgb_live.py` | the **gated** XGBoost override + shared feature builders |
| `train_xgb.py`, `xgb_features.py` | XGBoost training + walk-forward extractors |
| `build_ratings.py`, `refresh_data.py`, `refresh.py` | rating construction and the 6-hourly self-refresh |
| `fetch_oe.py`, `fetch_lol_players.py` | Oracle's Elixir ingestion (the LoL player data) |
| `backtest.py`, `tune.py` | walk-forward evaluation and hyperparameter fitting |
| `manual_trades.py` | human-entered bet tracking (separate table) |

**Feature builders in `xgb_live.py` are shared by training and inference on
purpose** — that's what makes train/serve drift structurally impossible. Don't
duplicate them.
