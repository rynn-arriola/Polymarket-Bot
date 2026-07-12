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
import re
from datetime import date
from pathlib import Path

OE_DIR = Path("data/oe")

# Google Drive file ids per year (from Oracle's Elixir; via the
# HerrKurz/Esports_Data_Pipeline config, verified 2026-07-09; 2026 id
# read from OE's public Drive folder 2026-07-12). Years not listed here
# are discovered live from the folder (_discover_drive_id), so a new
# year's file is picked up automatically each January.
DRIVE_IDS = {
    "2026": "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
    "2025": "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    "2024": "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    "2023": "1XXk2LO0CsNADBB1LRGOV5rUpyZdEZ8s2",
    "2022": "1EHmptHyzY8owv0BAcNKtkQpMwfkURwRy",
}

# Oracle's Elixir's public Drive folder (one CSV per year lives here).
OE_FOLDER_ID = "1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH"


def default_years(n: int = 4) -> list[str]:
    """Current year first, then the n-1 prior years — the CURRENT year is
    what keeps the LoL player model fresh, so it must always be in the list
    (a hardcoded year list is how the model silently went stale in 2026)."""
    y = date.today().year
    return [str(y - i) for i in range(n)]


def _discover_drive_id(year: str) -> str | None:
    """Look up a year's file id from OE's public Drive folder listing, for
    years not in DRIVE_IDS (i.e. every new season). Best-effort: any failure
    returns None and the caller reports the year unavailable this run."""
    import urllib.request
    url = f"https://drive.google.com/embeddedfolderview?id={OE_FOLDER_ID}#list"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"{year}: Drive folder listing failed ({str(e)[:50]})")
        return None
    # Pair each entry's id with ITS OWN title (a single spanning regex would
    # pair the page's first id with any later title), then exact-match.
    pairs = re.findall(r'file/d/([\w-]{20,})/view[^>]*>.*?flip-entry-title">([^<]+)<',
                       html, re.S)
    want = FNAME.format(year=year)
    for fid, name in pairs:
        if name.strip() == want:
            print(f"{year}: discovered Drive id {fid} from OE folder")
            return fid
    return None

# Known GitHub mirrors that committed a full-year CSV (raw.githubusercontent).
GITHUB_MIRRORS = {
    "2023": "https://raw.githubusercontent.com/Ashalo/LOLAnalysis/main/"
            "2023_LoL_esports_match_data_from_OraclesElixir.csv",
}

FNAME = "{year}_LoL_esports_match_data_from_OraclesElixir.csv"


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1_000_000


def acquire(year: str, max_age_days: int | None = None) -> bool:
    """Ensure a year's CSV is present; True if a usable file exists on return.

    max_age_days: if set and the local copy is older, refetch it — the CURRENT
    season's file grows all year, so the live refresh passes 1 for it (past
    years are immutable and never refetched). A refetch downloads to a temp
    file and only replaces the old copy on success, so a failed download
    (Drive quota, network) never destroys a good cached year."""
    import time
    OE_DIR.mkdir(parents=True, exist_ok=True)
    out = OE_DIR / FNAME.format(year=year)
    refreshing = False
    if _ok(out):
        age_d = (time.time() - out.stat().st_mtime) / 86400
        if max_age_days is None or age_d <= max_age_days:
            print(f"{year}: already present ({out.stat().st_size // 1_000_000} MB)")
            return True
        refreshing = True
        print(f"{year}: local copy {age_d:.1f}d old (> {max_age_days}d) — refetching")
    dst = out.with_name(out.name + ".tmp") if refreshing else out

    def _landed(source: str) -> bool:
        if not _ok(dst):
            if dst.exists():
                dst.unlink()  # remove a tiny quota-error HTML page etc.
            return False
        if refreshing:
            os.replace(dst, out)
        print(f"{year}: got {source} ({out.stat().st_size // 1_000_000} MB)")
        return True

    if year in GITHUB_MIRRORS:
        import urllib.request
        try:
            req = urllib.request.Request(GITHUB_MIRRORS[year],
                                         headers={"User-Agent": "DivergenceBot/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(dst, "wb") as f:
                f.write(resp.read())
            if _landed("GitHub mirror"):
                return True
        except Exception as e:
            print(f"{year}: GitHub mirror failed ({str(e)[:50]})")

    fid = DRIVE_IDS.get(year) or _discover_drive_id(year)
    if fid:
        try:
            import gdown
            gdown.download(id=fid, output=str(dst), quiet=True)
            if _landed("Google Drive"):
                return True
        except Exception as e:
            print(f"{year}: Drive blocked ({str(e).splitlines()[0][:50]})")

    if dst.exists() and dst != out:
        dst.unlink()
    if refreshing:
        print(f"{year}: refetch failed — keeping the previous copy (rerun later)")
        return True  # the old file is still present and usable
    print(f"{year}: UNAVAILABLE this run — rerun later (Drive quota resets ~daily)")
    return False


if __name__ == "__main__":
    import sys
    years = sys.argv[1:] or default_years()
    got = [y for y in years if acquire(y)]
    print(f"\nAvailable years in data/oe/: {got or 'none'}")
