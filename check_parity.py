"""Is the server running exactly what's on main?

Main and the server must never disagree. This compares every git-tracked
runtime file against the server's copy and reports three kinds of drift:

  DIFFERS  — content differs (the dangerous one: the server is running code
             that isn't on main, or main has a fix that was never deployed)
  MISSING  — tracked in git, absent on the server (never deployed)
  EXTRA    — a .py on the server that git doesn't know about (usually a stale
             copy from a mis-targeted scp; dead but confusing)

Line endings are normalized before hashing: the repo is checked out on Windows
(CRLF) and the server is LF, so a raw hash compares equal-content files as
different. Only real content drift is reported.

Exit code 0 = in sync, 1 = drift found (usable in a script).
"""

import hashlib
import os
import subprocess
import sys

SERVER = "root@142.93.58.166"
SSH_KEY = "~/.ssh/polybot_droplet"
REMOTE_DIR = "/root/divergence-bot"

# Tracked in git but deliberately NOT on the server: config templates. The real
# config.py lives only on the server (secrets) and only in git as an example.
EXPECTED_MISSING = {"credentials.example.py"}

# Files the server legitimately has that git doesn't track.
EXPECTED_EXTRA = {"config.py", "credentials.py"}

# GENERATED DATA — the server rebuilds these every 6h and its copies are
# SUPPOSED to be fresher than the local ones. Never scp them; never compare
# them. (model_params.json is NOT here: it's tuned input, deployed like code.)
GENERATED = {"elo_ratings.json", "elo_freshness.json", "lol_player_model.json"}


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Show the program and its error, not the (huge) remote script body.
        sys.exit(f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip()}")
    return r.stdout


def _ssh(remote_cmd: str) -> str:
    return _run(["ssh", "-i", SSH_KEY, "-o", "IdentitiesOnly=yes",
                 "-o", "ConnectTimeout=20", SERVER, remote_cmd])


def _norm_hash(data: bytes) -> str:
    """Hash with line endings normalized, so CRLF vs LF isn't false drift."""
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    # git ls-files yields paths relative to the repo ROOT, so read them from
    # there — otherwise running this from a subdirectory reports every file as
    # missing.
    os.chdir(_run(["git", "rev-parse", "--show-toplevel"]).strip())

    tracked = sorted(
        f for f in _run(["git", "ls-files"]).splitlines()
        if f.endswith((".py", ".txt", ".json"))
        and not f.startswith("xgb_models/")
        and f not in GENERATED
    )
    if not tracked:
        sys.exit("no tracked files found — are you in the repo root?")

    local = {}
    for f in tracked:
        try:
            with open(f, "rb") as fh:
                local[f] = _norm_hash(fh.read())
        except FileNotFoundError:
            print(f"  local file vanished (tracked but not on disk): {f}")

    # One ssh round-trip: hash every tracked path, and list every .py present.
    paths = " ".join(f"'{f}'" for f in local)
    out = _ssh(
        f"cd {REMOTE_DIR} && "
        f"for f in {paths}; do "
        f"  if [ -f \"$f\" ]; then printf '%s %s\\n' \"$f\" "
        f"    \"$(sed 's/\\r$//' \"$f\" | md5sum | cut -d' ' -f1)\"; "
        f"  else printf '%s ABSENT\\n' \"$f\"; fi; done; "
        f"echo '---INVENTORY---'; "
        f"find . -maxdepth 2 -name '*.py' -not -path './venv/*' "
        f"  -not -path './__pycache__/*' -not -path './elo/__pycache__/*' | sed 's|^\\./||'"
    )
    hashes_part, _, inventory_part = out.partition("---INVENTORY---")

    server = {}
    for line in hashes_part.strip().splitlines():
        name, _, h = line.rpartition(" ")
        server[name.strip()] = h.strip()

    differs, missing = [], []
    for f, lh in local.items():
        sh = server.get(f)
        if sh is None or sh == "ABSENT":
            missing.append(f)
        elif sh != lh:
            differs.append(f)

    server_pys = {p.strip() for p in inventory_part.strip().splitlines() if p.strip()}
    extra = sorted(server_pys - set(local) - EXPECTED_EXTRA)

    real_missing = [f for f in missing if f not in EXPECTED_MISSING]

    print(f"compared {len(local)} tracked files against {SERVER}:{REMOTE_DIR}")
    print(f"(skipped {len(GENERATED)} generated data files - server rebuilds "
          f"those every 6h and its copies are meant to be fresher)\n")
    for f in differs:
        print(f"  DIFFERS : {f}   <-- server is NOT running what's on main")
    for f in real_missing:
        print(f"  MISSING : {f}   <-- tracked in git, never deployed")
    for f in extra:
        print(f"  EXTRA   : {f}   <-- on server, not in git (stale scp? dead code?)")
    for f in (f for f in missing if f in EXPECTED_MISSING):
        print(f"  ok      : {f} absent on server (expected - template only)")

    drift = bool(differs or real_missing or extra)
    print()
    if drift:
        print("DRIFT - main and the server disagree. Reconcile before trusting either.")
    else:
        print("IN SYNC - every tracked file on the server matches main.")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
