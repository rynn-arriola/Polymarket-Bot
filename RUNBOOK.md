# OPERATOR RUNBOOK — what to watch, what to run, when

Your one-page reference. Server: `ssh root@142.93.58.166`, bot lives in
tmux session `bot`, code in `/root/divergence-bot`. Local dev copy:
`d:\polybot\divergence-bot` (Windows, PowerShell).

---

## THE GOLDEN RULES (read once, never break)

1. **NEVER copy any `positions.db` TO the server.** The server's is the live
   money record. Downloading it is fine; uploading, never.
2. **NEVER `scp -r` or `scp *` toward the server.** Always an explicit list
   of files.
3. **Config/code changes flow local → git → scp → restart.** Don't hand-edit
   files on the server (they get overwritten by the next deploy).
   **Never scp generated data** (`elo_ratings.json`, `elo_freshness.json`,
   `data/`): the server rebuilds those itself every 6h and its copies are
   fresher than yours. (If ever clobbered by accident they self-heal on the
   next refresh — unlike positions.db, which never does.)
4. **One strategy change per week**, decided from the edge report.
5. **When anything looks wrong: paste it to Claude** (Discord alert, digest,
   audit output, weird numbers). The triage maps live in memory.

---

## DAILY — nothing to run, just glance at Discord

| Channel | What good looks like | When to act |
|---------|---------------------|-------------|
| Status (every 5 min) | P&L/record ticking along | — |
| Settlements | a card per finished bet | a bet you KNOW settled has no card → paste to Claude |
| CLV report (4×/day) | 🟢 = market moves toward our bets | persistent 🔴 across days → mention it |
| Errors channel | quiet; 6pm digest small or green | any 🚨 CRITICAL → paste to Claude same day |

---

## WEEKLY — Sunday, ~2 minutes

```bash
ssh root@142.93.58.166
cd /root/divergence-bot
python3 edge_report.py
```
Paste the ENTIRE output to Claude → we make at most ONE config change.

---

## DEPLOYING (when Claude hands you files)

```powershell
cd d:\polybot\divergence-bot
scp <exact files Claude lists> root@142.93.58.166:/root/divergence-bot/
# elo/ files go to .../divergence-bot/elo/
```
Restart (needed for: divergence_bot.py, config.py, credentials.py, risk.py,
db.py, reporting.py, name_match.py, xgb_live.py — NOT for fetch_oe.py /
refresh_data.py, which run as fresh subprocesses):
```bash
ssh root@142.93.58.166
tmux attach -t bot        # Ctrl+C to stop
python3 divergence_bot.py # relaunch
# Ctrl+B then D to detach
```
After deploy: watch the log for the startup banner + one clean "Cycle done".

---

## CHECKS — run these when something seems off

```bash
cd /root/divergence-bot

# Are the books true? (drift, missed settlements, stuck rows)
python3 audit_reporting.py

# Where is the edge working? (same as the Sunday report)
python3 edge_report.py

# Recent warnings/errors without reading Discord
python3 divergence_bot.py errors

# Unmatched team/player names worth aliasing
python3 suggest_aliases.py

# What's open/pending right now
sqlite3 positions.db "SELECT market_slug,status,created_at FROM positions
  WHERE live=1 AND status IN ('open','pending') ORDER BY created_at DESC;"

# Is the bot alive / what's it doing
tmux attach -t bot        # look, then Ctrl+B D to leave it running
```

---

## EMERGENCIES

**Bot down / no Discord posts for >15 min:**
```bash
tmux attach -t bot        # crashed? you'll see the traceback — copy it for Claude
python3 divergence_bot.py # relaunch
```

**Droplet was rebooted (tmux is gone):**
```bash
tmux new -s bot
cd /root/divergence-bot && python3 divergence_bot.py
# Ctrl+B D
```

**STOP TRADING NOW (kill switch):**
```bash
# in tmux: Ctrl+C stops everything (open positions still settle on the
# exchange by themselves — stopping the bot never abandons money).
# To run but not trade: set LIVE = False in config.py locally, deploy, restart.
```

**🚨 "UNTRACKED order/position" alert:** don't panic — the bot already
blocked re-entry. Check the Polymarket app for that market, then paste the
alert to Claude; there's a standard reconcile procedure.

**"DAILY LOSS LIMIT hit" in the log:** not an emergency — that's the brake
working. New entries halt until midnight ET; open positions still settle.

---

## WHAT'S AUTOMATIC (no action ever)

- Market scan + entries + fills + cancels: every 60s
- Settlement detection + Discord cards: within ~1 min of exchange resolution
- P&L reconciliation to exchange-exact figures: within ~1h of each settlement
- Ratings/data refresh: every 6h (hot-reload, no restart)
- CLV capture: final minutes before every game start
- LoL 2026 data: retried every refresh until the Drive quota yields
- Daily loss brake, roster/injury/dormancy guards, price/spread gates

## WHERE DECISIONS STAND (see MODELS_TODO.md for the full board)

- CLV is the judge: ~2 weeks to per-sport verdicts → then the CLV auto-guard
- LoL XGBoost blend: proven, benched behind fresh data + CLV + your approval
- xgboost-dev branch NEVER merges to main without your explicit say-so
