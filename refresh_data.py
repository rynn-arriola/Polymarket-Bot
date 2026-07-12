"""One-shot data refresh: pull the latest results for every sport, rebuild
all Elo ratings + the LoL player sidecar, and print a freshness summary.

This is what keeps the models current. It is:
  - spawned automatically by the running bot every config.DATA_REFRESH_HOURS
    (the 24/7 self-refresh path — nothing else to set up on the server), and
  - runnable by hand or from cron/Task Scheduler if you prefer external
    scheduling: `python refresh_data.py`.

Cheap by design: history.cached_chunk freezes past chunks and refetches only
recent days, and the esports stores accumulate incrementally — so a refresh
is mostly cached re-reads plus the last few days of new games. Safe to run
often. Exits non-zero only on a hard failure (so a supervisor can tell).
"""

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh_data")


def main() -> int:
    t0 = time.monotonic()

    # LoL player data (Oracle's Elixir): idempotent; a no-op once the CSVs
    # are present, and the path that lands 2024/2025 when Drive frees up.
    try:
        import fetch_oe
        for year in ("2025", "2024", "2023"):
            fetch_oe.acquire(year)
    except Exception as e:
        log.warning(f"fetch_oe skipped: {e}")

    # Rebuild every sport's ratings from cached + freshly-pulled results.
    try:
        import build_ratings
        build_ratings.build_all()
    except Exception as e:
        log.error(f"build_ratings failed: {e}", exc_info=True)
        return 1

    log.info(f"Data refresh complete in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
