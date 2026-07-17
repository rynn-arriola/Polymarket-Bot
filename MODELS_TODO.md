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
- [x] **Phase 4 close-out DONE (2026-07-13)**: LoL blend = sole winner
      (awaiting activation gates); everything else ruled out on free data;
      final verdict written in XGBOOST_PLAN.md. No further feature
      experiments on current columns.

## Player-data collection (the "new data" program — 2026-07-16)

The LoL blend proved the pattern: player-level data is the only feature class
that ever beat Elo here. Per-title status of getting it (all sources probed
live 2026-07-16):

- [x] **LoL** — already have it (Oracle's Elixir CSVs; live blend feeds on it).
- [x] **dota2 collector BUILT** (this branch): OpenDota proMatches walk into a
      separate OpenDota-id store (`esports_dota2_od_store.json` — never joined
      with the ps* live store) + per-match lineup capture
      (`esports_dota2_lineups.json`, LoL game shape `{date, teams, winner}`,
      `load_dota_games()` reader). 300 lineup calls/run, 4 runs/day + walk
      stays under OpenDota's 2000/day. ~18mo backfill completes in ~3-4 weeks.
      Verified live (5 lineups, full 5v5, correct winner attribution; ps store
      untouched). **[ ] Needs merge to main + deploy before it accumulates on
      the server** — collection-only, nothing live reads it.
- [ ] **cs2 — source FOUND, collector not built**: bo3.gg match detail
      (`/api/v1/matches/{slug}?with=players`) returns the true historical
      10-player lineup keyless (verified on a 2020 match: real shox/kennyS-era
      G2/Vitality). CATCH: each player's `team_id` is his CURRENT team, so
      sides can't be split on old matches → **backfill impossible,
      forward-only collection works** (capture within days of the match while
      team_id is still true; the store walk already runs every 6h). Bonus
      column: `six_month_avg_rating` per player — a real skill feature. Build
      as the next collector.
- [ ] **valorant — NO free per-match player source**: vlr mirror has no
      match-detail endpoint (404), results rows carry no players; PandaScore
      free tier has none (per-game detail 403) and paid stats plans are
      betting-restricted. Team endpoint gives current roster only (already
      used by the guard). Parked until a source appears.
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
