"""In-process data-refresh manager for the 24/7 bot.

Every config.DATA_REFRESH_HOURS the bot spawns `refresh_data.py` as a
SEPARATE process (not a thread) and keeps trading while it runs. When the
child finishes, the bot hot-reloads the freshly-written ratings — no restart
needed, no pause in scanning. A subprocess (vs inline rebuild) means a
rebuild that hangs or crashes can never take the trading loop down with it,
and the loop stays responsive throughout.

Design choices for server robustness:
  - First refresh fires one full interval AFTER launch (ratings were just
    built at startup, so no need to rebuild immediately).
  - Only one refresh runs at a time; if a previous one is still going when
    the next is due, it's skipped (logged), not stacked.
  - A refresh that exits non-zero is logged and does NOT trigger a reload —
    the last-good ratings stay in use.
  - The child writes to the same files build_ratings always writes; reload
    tolerates a torn read (keeps the old ratings if the JSON won't parse).
"""

import logging
import subprocess
import sys
import time

import config

log = logging.getLogger("divergence_bot.refresh")


class DataRefresher:
    def __init__(self):
        self.interval = getattr(config, "DATA_REFRESH_HOURS", 6) * 3600
        self.last_started = time.monotonic()  # count from launch
        self.proc: subprocess.Popen | None = None
        self.log_path = "refresh_data.log"

    def _spawn(self):
        # Same interpreter, this project's dir (cwd is already the project).
        f = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "refresh_data.py"],
            stdout=f, stderr=subprocess.STDOUT,
        )
        self.last_started = time.monotonic()
        log.info(f"Data refresh started (pid {self.proc.pid}) — trading continues; "
                 f"ratings hot-reload when it finishes. Log: {self.log_path}")

    def tick(self, reload_fn):
        """Call once per scan cycle. Spawns a refresh when due; when a running
        one finishes cleanly, calls reload_fn() to swap in the new ratings.
        reload_fn must return True on a successful reload, False otherwise."""
        # A finished refresh? Reap it and reload.
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            if code == 0:
                ok = reload_fn()
                log.info(f"Data refresh finished — ratings {'reloaded' if ok else 'reload FAILED, keeping previous'}")
            else:
                log.warning(f"Data refresh exited {code} — keeping previous ratings (see {self.log_path})")
            return

        # Due for a new one?
        if self.proc is None and time.monotonic() - self.last_started >= self.interval:
            try:
                self._spawn()
            except Exception as e:
                log.error(f"Could not start data refresh: {e}")
                self.last_started = time.monotonic()  # back off a full interval
