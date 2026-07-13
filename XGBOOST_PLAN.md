# XGBoost work plan (branch: xgboost-dev)

## The premise, stated honestly

The XGBoost machinery is already built and trustworthy:
- `xgb_features.py` — walk-forward feature extraction, shares builders with inference (no train/serve drift)
- `train_xgb.py` — chronological train/val/test split, Platt calibration, Brier-vs-Elo on the identical test games, writes `beats_elo`
- `xgb_live.py` — gated inference: a model activates ONLY if it beat Elo out-of-sample AND is fresh

A prior test found XGBoost only **ties** Elo (NBA/ATP/WTA). The reason was not the
algorithm or the sample size — it was that **every feature fed to it was an Elo
transform** (`elo_gap`, `elo_exp`, ratings…). Trees cannot extract information that
isn't in the columns. Ten views of one number rediscover that number.

**Therefore this is a feature-acquisition project, not a modeling project.** The one
time anything beat Elo here was **LoL player-Elo** (Brier 0.2253 vs 0.2300) — because
player-level rating is *orthogonal* signal to team-level rating. That is the template:
find features carrying information Elo doesn't, feed team-Elo AND the new signal, let
the trees learn when to trust which, and let the existing gate decide if it ships.

## The gating principle (do not skip)

1. **Every candidate feature is judged by the existing gate** — walk-forward, Brier vs
   Elo on untouched recent games. No feature ships on intuition.
2. **CLV validates the base strategy in parallel.** A marginally better model does not
   rescue a strategy with no real edge. Live model *shipping* waits on CLV showing the
   current Elo edge is real. But offline feature research risks no money and proceeds now.

## Candidate orthogonal signals, ranked by promise

| Rank | Feature source | Sports | Why it could be orthogonal | Blocker / cost |
|------|----------------|--------|----------------------------|----------------|
| 1 | Player-level ratings as a feature | esports (dota2/cs2/valorant), LoL | proven pattern (LoL player-Elo beat team-Elo) | needs PER-MATCH lineups — **confirm PandaScore exposes them** |
| 2 | Series format (Bo1/Bo3/Bo5) | esports | best-of length is a huge variance driver Elo ignores | cheap if PandaScore carries `number_of_games` |
| 3 | Tournament tier / stakes | esports | teams tryhard differently by tier | cheap if in match payload |
| 4 | Fatigue (matches in last N days) | esports, NBA(have) | tournament grind, travel | derivable from our own store |
| 5 | Patch / map / map-pool | dota2/cs2/valorant | metagame shifts Elo can't see | medium; per-game detail |

Explicitly OUT of scope (for now):
- **Market-derived features** (opening line, line movement): we are trying to BEAT the
  market — training on its price makes the model predict the market, not the outcome.
- **Tennis**: players ARE the unit; player-Elo already IS the model. No orthogonal
  player signal to add.

## Phases

**Phase 0 — Confirm the premise. ✅ ANSWERED 2026-07-13: NO lineups on the free plan.**
Match payloads carry no player lists and the per-game detail endpoint returns HTTP 403
(paid tier). → The player-Elo path (3a) is DEAD for dota2/cs2/valorant on free data;
LoL keeps its player signal (Oracle's Elixir CSVs, independent of PandaScore).
**BUT the same probe confirmed the rank-2/3 features ARE in payloads we already fetch:**
`number_of_games` (Bo1/Bo3/Bo5) and `tournament.tier` sit in every /matches/past item.
→ **Phase 3b is promoted to the flagship path.**

**Phase 1 — Extend the harness to all sports. ✅ DONE 2026-07-12/13.**
BASE_FEATURES + esports/mlb/fwc extractors + `--offline` flag (esports train from the
local store without stalling on rate-limited source refreshes).

**Phase 2 — Control run, all 10 sports. ✅ DONE 2026-07-13.** As predicted, XGBoost on
Elo-only features ties Elo EVERYWHERE (every delta within ±0.0011 Brier, all under the
0.002 activation margin, all models correctly inactive):

| sport | Elo    | XGB    |   | sport    | Elo    | XGB    |
|-------|--------|--------|---|----------|--------|--------|
| nba   | 0.2095 | 0.2106 |   | dota2    | 0.2153 | 0.2148 |
| wnba  | 0.2132 | 0.2128 |   | cs2      | 0.2193 | 0.2194 |
| atp   | 0.2226 | 0.2221 |   | lol      | 0.2337 | 0.2340 |
| wta   | 0.2196 | 0.2201 |   | valorant | 0.2432 | 0.2432 |
| mlb   | 0.2469 | 0.2475 |   | fwc      | 0.2420 | 0.2459 |

This is the anchor: a future feature earns activation only by dragging a sport's XGB
Brier below Elo − 0.002 on the untouched test slice. Pipeline, gate, baseline: done.

**Phase 3 — The real experiments (each judged by the gate).**
- **3b (flagship now): match-context features for esports.** Bo-format
  (`number_of_games` — Bo1 variance vs Bo3/Bo5 skill expression is a real, Elo-blind
  effect), tournament tier, and fatigue (matches in last N days — derivable from our
  own store, no new data). COST: the store keeps only (date, winner, loser) today;
  format/tier need a store-schema extension + one-time historical re-walk (~1.6k
  requests, same resumable pattern as the 2026-07-12 deep backfill).
- 3a (player-aggregate features): **LoL only** (Oracle's Elixir per-game lineups).
  Blocked for other esports unless a paid tier or new free source appears.

**Phase 4 — Decide and document.**
Any sport whose model clears `beats_elo` gets its file shipped; `xgb_live` activates it
automatically on the server. Everything else stays on Elo. Record what won and lost so
we never re-run a dead end (extends the "XGBoost verdict" note).

## PROGRAM COMPLETE (2026-07-13) — final verdict

Phase 3 ran to the end of the free data. Results:
- Recency weighting: DEAD, all 10 sports (staleness already lives in elo_exp).
- LoL player blend: **THE WINNER** — beats team Elo +0.0139 and player-Elo
  +0.0044, ship-ready behind its activation gates (fresh 2026 OE data, retrain,
  CLV verdict, explicit approval).
- Esports context (bo/tier/fatigue): DEAD on dota2 (+0.0006), valorant
  (val-rejected) and cs2 (+0.0006, 68k rows) — series-level results already
  absorb Bo-variance into rating dynamics.

CONCLUSION: on free data, Elo is the ceiling everywhere except where
player-level data exists (LoL). The proven pattern — orthogonal PLAYER data
beats team ratings — generalizes to dota2/cs2/valorant only via paid lineups
(PandaScore stats plans, restricted for betting usage; see MODELS_TODO).
Do not run further feature experiments on the current columns: the ledger
says everything tried, and re-running dead ends is how noise gets shipped.

Side profits of the program, banked for the LIVE models: valorant store
5.6k->17.4k and cs2 52k->88.6k matches (retune candidates on MODELS_TODO),
plus the dormancy patch that came out of the same non-stationarity research.
