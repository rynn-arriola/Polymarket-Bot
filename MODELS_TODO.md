# All-models TODO (living doc — updated 2026-07-16)

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
| cs2 | Deep PandaScore + first roster guard | [ ] same; NEW: [ ] retune k/calibration on the enrichment-deepened store (52k->88.6k matches) — pairs with valorant's retune as one evidence-gated change |
| valorant | Weakest model | [x] deep backfill DONE via exp-3 enrichment (5.6k->17.4k matches, to 2021). NEW follow-ups: [ ] retune valorant k/calibration on the deep store (Elo val Brier already 0.2325->0.2256 untuned) — next-week's change candidate; [ ] ship enriched stores to the server (its own fetch can never deep-backfill: early-stop) or run enrichment there |
| LoL | **XGB player blend LIVE since 2026-07-14** (OE sidecar, gated) | [ ] watch its CLV + win rate — backtest +0.008 Brier is a hypothesis, not a fact |
| ITF | No data source, markets skipped | [ ] re-check for a free ITF source occasionally (low) |

## XGBoost layer (xgboost-dev)

- [x] Pipeline for all 10 sports + honest baseline (all tie Elo) — DONE
- [x] Research protocol (9 pitfalls, ledger, 5-seed rule) — DONE
- [x] Exp 1 recency weighting — DEAD, ledgered
- [x] Exp 2 LoL player blend — **FIRST GATE CLEAR** (+0.0044 vs player-Elo,
      +0.0139 vs team-Elo)
- [x] LoL blend deployment path — ship-ready (sidecar parity 1e-6, gated
      inference, harness-trained)
- [x] **LoL blend ACTIVATED 2026-07-14** — live on main/server, gated; CLV
      verdict pending (see decision board)
- [x] Exp 3 esports context features — DEAD all three titles (dota2 +0.0006,
      valorant val-rejected, cs2 +0.0006 on 68k rows); ledgered
- [x] **Original Phase 4 close-out DONE (2026-07-13)**: LoL blend was the sole
      winner on the sources known then. The verdict against further experiments
      on the same columns still stands.
- [ ] **Player-data phase reopened 2026-07-18** after finding independent free
      lineup datasets for Dota 2, CS2, and Valorant. Dota/CS2 bootstrap and
      source-integrity tooling are built; neither model is eligible for live use.

## Player-data collection (the "new data" program — updated 2026-07-18)

The LoL blend proved the pattern: player-level data is the only feature class
that ever beat Elo here. Per-title status after API and published-dataset
probes through 2026-07-18:

- [x] **LoL** — already have it (Oracle's Elixir CSVs; live blend feeds on it).
- [x] **dota2 collector BUILT + ACCELERATED**: OpenDota proMatches walk into a
      separate OpenDota-id store (`esports_dota2_od_store.json` — never joined
      with the ps* live store) + per-match lineup capture
      (`esports_dota2_lineups.json`, LoL game shape `{date, teams, winner}`,
      `load_dota_games()` reader). The 2026-07-18 audit found 39,917 stored
      matches but only 1,006 lineups (38,911 pending). The old no-new-matches
      path exhausted calls walking known pages and hit HTTP 429 before lineup
      capture. Main now stops after two known pages once backfill is complete
      and captures 450 lineups/run, 4 runs/day, reducing ETA from ~32 to ~22
      days while staying under OpenDota's 2,000 calls/day.
- [x] **dota2 historical bootstrap BUILT, RESEARCH ONLY**: published pro-match
      Parquet has 193,773 raw maps and 191,202 usable merged games with stable
      player ids. It ends 2024-10-15, leaving a 638-day gap to the then-current
      forward lineup store. Same-source chronological result: player Elo
      0.2356 vs team Elo 0.2458 over 23,898 tests. This is promising but is not
      comparable to production PandaScore's 0.2146 series-level Brier and does
      not clear the live gate.
- [x] **cs2 collector BUILT + CANONICALIZED**: forward-only bo3.gg lineup
      collection (the LIST endpoint carries all 10 players/row — no per-match
      calls) into
      `esports_cs2_lineups.json`, 30-day age window, tier s-c,
      `load_cs2_games()` reader. Server audit 2026-07-18: 473 valid matches,
      4,307 canonical nickname player entries, zero legacy/invalid rows. The
      collector prunes unresolved legacy rows and recent ones remain eligible
      for a later refetch.
- [x] **cs2 historical bootstrap BUILT, NO VERDICT**: the candidate Kaggle
      HLTV table was rejected because current rosters leak backward into old
      matches. Replacement `blanchon/cs2_dataset_demo` is replay-grounded and
      contributes 1,000 exact maps (2026-03-23 through 2026-04-20). Combined
      audit had 1,472 maps, a 59-day source gap, and only 83 eligible test
      predictions versus the 100 minimum. Tournament-data usage review is also
      required before any live use.
- [x] **valorant free source FOUND, build pending**:
      `ryanluong1/valorant-champion-tour-2021-2023-data` supplies per-map
      five-player lineups and stats, MIT licensed, and was current through
      2026-06-26 when verified. Required safeguards: order chronologically by
      VLR Match ID because rows have no dates, join score/stat rows on Team IDs
      because about 7% of names mismatch, account for missing China-hosted
      stats, and enforce a roughly monthly freshness gate.
- [ ] Traditional sports (NBA/MLB/tennis...) player data — later; esports
      first (roster churn makes it matter most here).

## Explicitly parked

- Weekly drawdown circuit-breaker (beyond daily halt) — offered, not requested
- Discord webhook retry on failed posts — cosmetic
- phi-det permanent estimate line in audit — exchange quirk, harmless
- **Paid data (researched 2026-07-13, parked until CLV + scale justify):**
  buy only when monthly profit > ~3x the subscription AND the purchase maps
  to a proven model class. Ranked menu: (1) Goalserve Tennis ~$150/mo — the
  one concrete unlock (ITF/Challenger model = new markets); (2) PandaScore
  paid €150-400/mo PER GAME — kills rate limits, but stats/lineup plans are
  RESTRICTED TO NON-BETTING USAGE (sales conversation required for us);
  (3) Sportradar/Abios/GRID enterprise = the true "one API", $1k-5k+/mo,
  not at this bankroll. Free first: Sackmann WTA/ITF CSV probe ($0).
