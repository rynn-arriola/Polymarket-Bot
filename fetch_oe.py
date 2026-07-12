"""Acquire Oracle's Elixir LoL match-data CSVs into data/oe/.

Oracle's Elixir distributes via Google Drive, whose anonymous downloads hit
a hard ~24h per-file quota ("Too many users") on the popular recent files.
So this tries, per year, in order:
  1. already-present local file (skip)
  2. a GitHub mirror that committed that year's CSV (no quota)
  3. Google Drive via gdown (works when the file's quota has headroom)

Idempotent and resumable — rerun daily (e.g. Task Scheduler); each year lands
the first time any source succeeds, and the LoL player-Elo model picks up
whatever years are present. Verified 2026-07-09: 2023 available via GitHub
mirror; 2024/2025 Drive-quota-blocked that day (they resolve on a later run).
"""

import os
from pathlib import Path

OE_DIR = Path("data/oe")

# Google Drive file ids per year (from Oracle's Elixir; via the
# HerrKurz/Esports_Data_Pipeline config, verified 2026-07-09).
DRIVE_IDS = {
    "2025": "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    "2024": "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    "2023": "1XXk2LO0CsNADBB1LRGOV5rUpyZdEZ8s2",
    "2022": "1EHmptHyzY8owv0BAcNKtkQpMwfkURwRy",
}

# Known GitHub mirrors that committed a full-year CSV (raw.githubusercontent).
GITHUB_MIRRORS = {
    "2023": "https://raw.githubusercontent.com/Ashalo/LOLAnalysis/main/"
            "2023_LoL_esports_match_data_from_OraclesElixir.csv",
}

FNAME = "{year}_LoL_esports_match_data_from_OraclesElixir.csv"


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1_000_000


def acquire(year: str) -> bool:
    OE_DIR.mkdir(parents=True, exist_ok=True)
    out = OE_DIR / FNAME.format(year=year)
    if _ok(out):
        print(f"{year}: already present ({out.stat().st_size // 1_000_000} MB)")
        return True

    if year in GITHUB_MIRRORS:
        import urllib.request
        try:
            req = urllib.request.Request(GITHUB_MIRRORS[year],
                                         headers={"User-Agent": "DivergenceBot/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(out, "wb") as f:
                f.write(resp.read())
            if _ok(out):
                print(f"{year}: got GitHub mirror ({out.stat().st_size // 1_000_000} MB)")
                return True
        except Exception as e:
            print(f"{year}: GitHub mirror failed ({str(e)[:50]})")

    fid = DRIVE_IDS.get(year)
    if fid:
        try:
            import gdown
            gdown.download(id=fid, output=str(out), quiet=True)
            if _ok(out):
                print(f"{year}: got Google Drive ({out.stat().st_size // 1_000_000} MB)")
                return True
            if out.exists():
                out.unlink()  # remove the tiny quota-error HTML page
        except Exception as e:
            print(f"{year}: Drive blocked ({str(e).splitlines()[0][:50]})")

    print(f"{year}: UNAVAILABLE this run — rerun later (Drive quota resets ~daily)")
    return False


if __name__ == "__main__":
    import sys
    years = sys.argv[1:] or ["2025", "2024", "2023"]
    got = [y for y in years if acquire(y)]
    print(f"\nAvailable years in data/oe/: {got or 'none'}")
