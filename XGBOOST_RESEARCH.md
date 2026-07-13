# XGBoost research: structure, pitfalls, and the rules for Phase 3

Companion to XGBOOST_PLAN.md. The plan says WHAT to try; this says HOW to try it
without fooling ourselves. Sports-outcome prediction with GBTs is a well-trodden
area — the literature's consensus recipe is exactly ours (ratings-as-features +
gradient boosting + temporal splits), and its failure modes are well documented.
Every experiment on this branch follows the rules below.

---

## Pitfall catalog, audited against our harness

### P1. Temporal leakage (the classic killer)
Random/shuffled CV lets future games inform past predictions; contemporaneous
features (computed WITH the game's outcome) do the same.
**Us: HANDLED.** Chronological 70/15/15 split, never shuffled; extractors emit
strictly pre-game state via the adapters' own walk-forward replays (shared with
inference, so no train/serve drift). Early stopping monitors the val slice, not test.
**Residual risk:** Elo K-factors were tuned (tune.py) over history that overlaps our
val/test windows. This slightly flatters the ELO baseline, not XGB — conservative
direction, acceptable.

### P2. Test-set erosion (multiple comparisons)
Every experiment that peeks at the same held-out slice burns it a little; run ten
feature ideas against one test set and the "winner" is likely luck. The dota2
0.0005 noise-win that tripped the old gate is this in miniature.
**Us: PARTIALLY HANDLED** (BEAT_MARGIN=0.002 raises the bar).
**RULES:**
- All iteration decisions use the VAL slice. The TEST slice is read ONCE per
  experiment, as the final verdict.
- Every test read is logged in the "Test-read ledger" below. If an experiment
  needs a second test read, the test window must be moved forward first.

### P3. Single-split variance / seed luck
One chronological split + one seed = a high-variance estimate; a 0.003 "win" can
be seed noise.
**Us: NOT HANDLED YET.**
**RULES:**
- A gate-clearing result must hold across 5 seeds: median delta > BEAT_MARGIN
  and no seed materially negative.
- Final verdicts additionally use 3 expanding-window folds (train→val→test rolled
  forward) rather than the single split, when a result is close to the margin.

### P4. Non-stationarity / concept drift (the betting-domain enemy #1)
Esports metas shift with patches; rosters turn over; leagues reorganize. A model
trained on 2015-2022 dota2 may describe a game that no longer exists. GBT models
"trained under false stationarity assumptions become obsolete over time."
**Us: PARTIALLY HANDLED** (season regression in Elo; XGB_STALE_DAYS=45 retrain
cadence; deep-history K retuned on the full window).
**RULES:**
- Recency sample-weighting (exponential decay on game age) is its own controlled
  experiment — likely the cheapest real win available, and it applies to EVERY
  sport with deep history, not just esports.
- If a context feature only exists for recent history (e.g. bo_format after a
  store re-walk), older rows carry NaN — see P6 — never a fabricated value.

### P5. Calibration (probabilities, not rankings)
Raw GBT outputs are poorly calibrated, and we trade on the PROBABILITY (divergence
vs price), not the ranking — miscalibration converts directly into fake edge.
**Us: HANDLED.** Platt on val, kept only if it helps holdout; the divergence
pipeline applies an identity Elo calibration on XGB sports so nothing double-corrects.
**RULE:** never isotonic on our sample sizes (it overfits below ~10k); Platt only.

### P6. Missing values and train/serve consistency
XGBoost handles NaN natively (learned default direction) — but only if training
and inference BOTH pass NaN for "unknown". Filling with 0 poisons the tree splits
(0 is a meaningful value for gaps/format).
**Us: RULE FOR PHASE 3:** historical rows lacking bo_format/tier feed float('nan'),
and xgb_live builders emit float('nan') when live context is unavailable. The
feature-dict contract must be exercised by a test that includes a NaN row.

### P7. Symmetry
P(A beats B) should equal 1 − P(B beats A); trees don't guarantee it. Our
alphabetical-order convention gives a consistent frame, and gap features
(elo_gap, rating diffs) are antisymmetric by construction — but raw per-side
features (rating_a, rating_b) let frame-dependent bias creep in.
**RULES:** prefer difference/ratio features for new signals (fatigue_diff, not
fatigue_a+fatigue_b alone); if a model ever ships, verify f(A,B)+f(B,A)≈1 on a
sample as a sanity test.

### P8. Hyperparameter thrash on tiny validation sets
Grid-searching depth/eta/lambda against a 300-row val slice (WNBA!) is fitting
noise. The literature's winning pattern: conservative fixed params, spend effort
on FEATURES.
**Us: HANDLED** (fixed shallow config: depth 3, eta 0.03, min_child_weight 20,
subsample/colsample 0.8, early stopping).
**RULE:** hyperparameters stay frozen through Phase 3. One exception allowed
AFTER a feature win: monotone_constraints on elo_exp/elo_gap (a pure regularizer,
domain-justified: better rating should never predict worse odds).

### P9. Drift after shipping
A model that clears the gate today decays.
**Us: PARTIALLY HANDLED** (XGB_STALE_DAYS freshness gate auto-deactivates old
models; retraining is manual).
**RULE:** if a model ever activates, add its sport to a weekly comparison —
recorded model_prob vs outcomes vs what Elo would have said (both are in the
positions table already) — so decay is observed, not assumed.

---

## The experiment protocol (every Phase-3 run)

1. Write the hypothesis first: WHICH feature, WHY it's orthogonal to Elo, expected
   effect size. (No fishing expeditions.)
2. Build features NaN-safe (P6), difference-form where possible (P7).
3. Iterate on VAL only (P2). Fixed hyperparameters (P8).
4. Verdict: ONE test read, 5 seeds (P3), logged in the ledger below.
5. Close to the margin → expanding-window folds before believing it.
6. Winner ships only via the existing gate + beat margin; loser gets its verdict
   recorded in XGBOOST_PLAN.md so it is never re-run on the same data.

## Test-read ledger

| date | experiment | sport(s) | test Brier (XGB vs Elo) | verdict |
|------|-----------|----------|--------------------------|---------|
| 2026-07-13 | Elo-only baseline (Phase 2) | all 10 | ties everywhere (±0.0011) | anchor set |
| 2026-07-13 | Recency weighting (exp 1) | 7 test reads (mlb/nba/fwc: val picked None, no read) | best median delta +0.0008 (lol), worst −0.0019 (valorant) | **DEAD — no gate clears.** Val Briers for None vs 730d were rounding-level ties on the deep-history sports: old data isn't hurting XGB, because elo_exp already carries Elo's sequential recency. Do not re-run on these features. |
| 2026-07-13 | LoL player-aggregate features (exp 2) | lol (OE per-game universe, 25.9k rows, 1 test read, 5 seeds) | XGB 0.2220 (range .2217-.2223) vs team Elo 0.2359 (**+0.0139**) vs player Elo 0.2263 (**+0.0044**, n=3873) | **FIRST GATE CLEAR — beats team Elo 7x margin AND the player-Elo incumbent 2x margin, every seed positive.** Gain-importance confirms blending: p_gap/p_exp dominate, elo_exp/elo_gap second. NOT deployable as-is: training's team features come from an OE-games engine, live's from the Leaguepedia match engine (train/serve drift) — shipping needs the sidecar extended to carry OE-consistent team+player state, and inherits the OE freshness gate. Held for CLV verdict + explicit user approval per plan. |
| 2026-07-13 | Esports context features (exp 3: bo_format/tier/fatigue) | dota2 (1 test read), valorant (val-rejected, no read); cs2 test read +0.0006 (68k rows, 100% context) | dota2 +0.0006; valorant val-rejected; cs2 +0.0006 | **DEAD — full negative across all three titles.** Gain importance: elo features ~60x the context features — bo/tier/fatigue carry almost nothing Elo doesn't. Likely cause: store rows are SERIES results, so Bo-variance is partially absorbed into rating dynamics already. Side discovery: the enrichment tripled valorant's store (5.6k->17.4k, to 2021) and its Elo baseline improved (val Brier 0.2325->0.2256) — live-model retune candidate, see MODELS_TODO. |

## Priority order for Phase 3 (updated by this research)

1. ~~**Recency weighting**~~ — RUN 2026-07-13, DEAD (see ledger). The negative
   result is itself informative: staleness is already priced into elo_exp, so
   non-stationarity is not where XGB's headroom is.
2. **LoL player-aggregate features** — proven-orthogonal signal, data local. NOW NEXT.
3. **Esports context features** (bo_format/tier/fatigue) — needs store extension
   + historical re-walk.
