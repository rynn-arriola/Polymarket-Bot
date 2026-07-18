"""Pull the server's live state DOWN to local. Never pushes. Run daily.

Two jobs:

  1. **Keep local from going stale.** The server rebuilds ratings every 6h, so
     its elo_ratings.json / elo_freshness.json / lol_player_model.json are
     always fresher than the local copies. Analysis run against stale local
     ratings is analysis of a bot that doesn't exist.
  2. **Back up the live money record.** positions.db exists in exactly one
     place: the droplet. If that droplet dies, the entire trading history dies
     with it. This is the only copy that isn't on it.

## This script CANNOT write to the server. That is the point.

It runs only read-only remote commands: a `sqlite3 .backup` into /tmp and the
reporting-integrity audit, plus cleanup of that temporary snapshot. Everything
else is `scp` in the server -> local direction. It never writes to
/root/divergence-bot, and it must never be "extended" to. Local is downstream
of the server, always:

    server  --->  local        OK, this script
    local   --->  server       NEVER for positions.db or generated data

Local positions.db (dry-run/dev, live=0) is NOT touched. The live DB lands in
server_mirror/, a separate read-only-by-convention copy, so the live and
dry-run databases can never be confused for each other.

Usage:  python pull_server_state.py
"""

import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER = "root@142.93.58.166"
SSH_KEY = "~/.ssh/polybot_droplet"
REMOTE_DIR = "/root/divergence-bot"
REMOTE_TMP = "/tmp/polybot_snapshot.db"

MIRROR = Path(__file__).resolve().parent / "server_mirror"
SNAPSHOTS = MIRROR / "snapshots"
KEEP_SNAPSHOTS = 30

# Generated on the server, rebuilt every 6h — its copies are always fresher.
GENERATED = ["elo_ratings.json", "elo_freshness.json", "lol_player_model.json",
             "valorant_player_model.json"]

SSH_OPTS = ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=20"]


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip()}")
    return r.stdout


def _ssh(remote_cmd: str) -> str:
    return _run(["ssh", *SSH_OPTS, SERVER, remote_cmd])


def _scp_down(remote_path: str, local_path: Path) -> None:
    _run(["scp", *SSH_OPTS, f"{SERVER}:{remote_path}", str(local_path)])


def pull_db() -> None:
    """Consistent copy of the live DB, even though the bot is writing to it.

    positions.db is in WAL mode: a plain scp of the .db file can miss commits
    still sitting in the -wal, or catch a torn page mid-write. `sqlite3
    .backup` takes a proper online snapshot (read-only w.r.t. the source), so
    what we pull is always a coherent database.
    """
    print("positions.db (live money record)")
    _ssh(f"cd {REMOTE_DIR} && sqlite3 positions.db \".backup {REMOTE_TMP}\"")

    staged = MIRROR / "positions.db.incoming"
    try:
        _scp_down(REMOTE_TMP, staged)

        # Verify BEFORE replacing the previous good mirror. A corrupt or
        # truncated pull must never destroy the last known-good backup. A file
        # too mangled to open as SQLite at all raises here rather than
        # returning "not ok", so catch that too.
        con = None
        try:
            con = sqlite3.connect(staged)
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                sys.exit("  FAILED integrity check - previous mirror left intact")
            rows = con.execute("SELECT COUNT(*) FROM positions WHERE live=1").fetchone()[0]
            settled = con.execute(
                "SELECT COUNT(*) FROM positions WHERE live=1 AND status IN "
                "('won','lost','push')").fetchone()[0]
            open_ = con.execute(
                "SELECT COUNT(*) FROM positions WHERE live=1 AND status='open'").fetchone()[0]
        except sqlite3.DatabaseError as e:
            sys.exit(f"  BAD DOWNLOAD ({e}) - previous mirror left intact, "
                     f"nothing changed. Just run it again.")
        finally:
            if con is not None:
                con.close()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        snapshot = SNAPSHOTS / f"positions_{stamp}.db"
        shutil.copyfile(staged, snapshot)
        staged.replace(MIRROR / "positions.db")   # atomic swap into place
        print(f"  ok: {rows} live rows ({settled} settled, {open_} open)")
        print(f"  snapshot: {snapshot.relative_to(MIRROR.parent)}")
    finally:
        staged.unlink(missing_ok=True)
        _ssh(f"rm -f {REMOTE_TMP}")


def pull_generated() -> None:
    """Ratings/model files the server rebuilds every 6h."""
    print("\ngenerated data (server rebuilds these every 6h)")
    for name in GENERATED:
        dest = MIRROR / name
        _scp_down(f"{REMOTE_DIR}/{name}", dest)
        print(f"  ok: {name} ({dest.stat().st_size:,} bytes)")


def audit_reporting() -> int:
    """Run the server-side, read-only reporting audit and preserve its output."""
    print("\nreporting-integrity audit (live DB + exchange)")
    result = subprocess.run(
        ["ssh", *SSH_OPTS, SERVER,
         f"cd {REMOTE_DIR} && python3 audit_reporting.py"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode:
        print(f"  FAILED: audit exited {result.returncode}; backup remains valid")
    return result.returncode


def prune() -> None:
    snaps = sorted(SNAPSHOTS.glob("positions_*.db"))
    for old in snaps[:-KEEP_SNAPSHOTS]:
        old.unlink()
    kept = len(snaps) - max(0, len(snaps) - KEEP_SNAPSHOTS)
    print(f"\n{kept} snapshot(s) retained (keeping newest {KEEP_SNAPSHOTS})")


def main() -> int:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    print(f"pulling FROM {SERVER}:{REMOTE_DIR}  (one-way; nothing is ever "
          f"written to the server)\n")
    pull_db()
    pull_generated()
    prune()
    audit_exit = audit_reporting()
    print(f"\ndone. Live DB mirrored to {MIRROR.name}/positions.db - this is a "
          f"BACKUP, not an input.\nLocal positions.db (dry-run) was not touched.")
    return audit_exit


if __name__ == "__main__":
    sys.exit(main())
