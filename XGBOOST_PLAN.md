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

**Phase 0 — Confirm the premise (one probe, gates everything).**
Does PandaScore expose per-match player lineups for dota2/cs2/valorant? (The probe was
rate-limited on 2026-07-12; retry when quota resets.) YES → rank-1 path is real and
becomes the flagship. NO → fall back to context features (ranks 2-4) + LoL-only player
signal.

**Phase 1 — Extend the harness to esports (pure infra, no risk).**
`train_xgb.load_matrix` + `xgb_features` currently cover only nba/wnba/atp/wta. Add an
esports extractor so esports models can even be trained/tested. Wire `xgb_live` feature
lists for the esports titles.

**Phase 2 — Control run on the deepened data.**
dota2/cs2 now have 35k/52k matches. Re-run XGB on the CURRENT (Elo-only) features as the
honest control. Expectation: still ties Elo. Cheap, and it anchors the comparison.

**Phase 3 — The real experiments (each judged by the gate).**
- 3a. Player-aggregate features (mean/min/spread of lineup player ratings) alongside
  team-Elo — the rank-1 bet, if Phase 0 says lineups exist.
- 3b. Context features: Bo-format, tier, fatigue.
- Test each addition independently so we learn WHICH signal (if any) carries edge.

**Phase 4 — Decide and document.**
Any sport whose model clears `beats_elo` gets its file shipped; `xgb_live` activates it
automatically on the server. Everything else stays on Elo. Record what won and lost so
we never re-run a dead end (extends the "XGBoost verdict" note).

## First concrete step
Retry the Phase-0 lineup probe once the PandaScore hourly quota clears (~1h from the
2026-07-12 backfill). That single fact decides whether the flagship path exists.
