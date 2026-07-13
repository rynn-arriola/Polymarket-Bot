# All-models TODO (living doc — updated 2026-07-13)

One list, every model, so nothing lives only in chat history. Ordered inside
each section by value. Rules that govern everything here: one live strategy
change per weekly review; CLV is the judge of edge; xgboost-dev never merges
to main without explicit approval.

## Cross-cutting (do these regardless of sport)

- [ ] **Confirm server deploy state** — dota2/cs2 PandaScore models, roster
      guards, dormancy patch are merged on main but server confirmation is
      outstanding (vlr-500s in the Jul-13 digest say elo/ files hadn't landed).
      `grep -c pandascore_enabled /root/divergence-bot/elo/rosters.py` (0 = not deployed).
- [ ] **Weekly edge report** (Sundays / every ~25 settled): paste output, ONE
      config change max, per edge-report-workflow.
- [ ] **Per-sport CLV guard** (~2 weeks out, once CLV n≥10-15/sport): auto-pause
      sports whose CLV stays negative. The self-adjusting endgame.
- [ ] **systemd unit for the bot** — droplet has pending kernel updates; tmux
      dies on reboot. 30 minutes, prevents silent downtime.

## Live Elo models (main)

| Model | State | TODO |
|-------|-------|------|
| ATP | Tier 1, nothing needed | — watch CLV only |
| WTA | Weak surface signal (ESPN has no labels; weight 0.15 vs ATP 0.4) | [ ] probe JeffSackmann/tennis_wta CSVs (free, surface-labeled) — could lift WTA toward ATP quality; verify coverage/freshness first |
| NBA | Dormant (offseason) | [ ] pre-season checkup (~Oct): ESPN feeds still alive, retune k on new season |
| WNBA | In season, small n | — watch CLV only |
| MLB | Best calibration, sharpest market | — most likely first CLV-pause candidate; no model work |
| FWC | World Cup ends 2026-07-19 | [ ] post-final: sport goes dormant — nothing to do, volume just stops |
| dota2 | Deep PandaScore + dormancy patch | [ ] deploy confirmation (above); then — watch CLV |
| cs2 | Deep PandaScore + first roster guard | [ ] same |
| valorant | Weakest model | [x] deep backfill DONE via exp-3 enrichment (5.6k->17.4k matches, to 2021). NEW follow-ups: [ ] retune valorant k/calibration on the deep store (Elo val Brier already 0.2325->0.2256 untuned) — next-week's change candidate; [ ] ship enriched stores to the server (its own fetch can never deep-backfill: early-stop) or run enrichment there |
| LoL | Team-Elo live; player-Elo benched on stale data | [ ] auto: 2026 OE CSV lands → sidecar rebuilds → player-Elo reactivates; blend model activation is the XGB checklist below |
| ITF | No data source, markets skipped | [ ] re-check for a free ITF source occasionally (low) |

## XGBoost layer (xgboost-dev)

- [x] Pipeline for all 10 sports + honest baseline (all tie Elo) — DONE
- [x] Research protocol (9 pitfalls, ledger, 5-seed rule) — DONE
- [x] Exp 1 recency weighting — DEAD, ledgered
- [x] Exp 2 LoL player blend — **FIRST GATE CLEAR** (+0.0044 vs player-Elo,
      +0.0139 vs team-Elo)
- [x] LoL blend deployment path — ship-ready (sidecar parity 1e-6, gated
      inference, harness-trained)
- [ ] **LoL blend ACTIVATION checklist**: (1) fresh 2026 OE data (auto-retrying),
      (2) rebuild sidecar + retrain, (3) CLV verdict positive, (4) explicit
      approval → merge + scp xgb_models/lol.* + sidecar
- [ ] **Exp 3 (IN PROGRESS): esports context features** — bo_format + tier +
      fatigue for dota2/cs2/valorant:
      - [ ] store schema: fetcher keeps number_of_games + tournament tier per
            match (tolerant readers; old rows -> NaN features per P6)
      - [ ] resumable enrichment re-walk (~1.6k rate-limited requests; also
            deepens valorant to full history as a side effect)
      - [ ] fatigue features from the store itself
      - [ ] inference wrinkle: upcoming-match bo_format via PandaScore
            upcoming lookup (cached per cycle)
      - [ ] train dota2/cs2/valorant on BASE + context; gate judges; ledger
- [ ] **Phase 4 close-out** after exp 3: ship what cleared (with post-ship
      weekly model-vs-Elo monitoring), write the final verdict for what
      didn't, STOP on free data (remaining paths are paid-tier)

## Explicitly parked

- Weekly drawdown circuit-breaker (beyond daily halt) — offered, not requested
- Discord webhook retry on failed posts — cosmetic
- phi-det permanent estimate line in audit — exchange quirk, harmless
- Paid PandaScore tier (per-match lineups for dota2/cs2/valorant) — only if
  exp 3 fails AND LoL blend proves live value
