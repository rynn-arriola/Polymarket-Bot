"""Standalone runner: backfill LoL per-game player rows month by month.
Each completed month caches immutably (data/cache/lol_players_YYYYMM.json),
so this is fully resumable — rerun until all months are cached, then
`python compare_lol_models.py` gives the player-vs-team verdict.
"""

import sys
from datetime import date, timedelta

from elo import history, lol_players

if __name__ == "__main__":
    start = date.today() - timedelta(days=lol_players.BACKFILL_DAYS)
    total = 0
    for c_start, c_end in history._month_chunks(start, date.today()):
        got = history.cached_chunk(
            f"lol_players_{c_start.strftime('%Y%m')}", c_end,
            lambda cs=c_start, ce=c_end: lol_players._fetch_month(cs, ce))
        total += len(got)
        print(f"{c_start.strftime('%Y-%m')}: {len(got)} rows (running total {total})", flush=True)
    print(f"DONE: {total} player rows", flush=True)
