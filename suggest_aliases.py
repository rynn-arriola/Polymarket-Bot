"""Alias-harvesting helper.

Scans the bot log for unmatched names (the "no Elo match for '...'" warnings)
and RE-CHECKS each against the CURRENT matcher + ratings. This matters because
the log is cumulative: a name skipped hundreds of times before a matcher
improvement is already handled now, and shouldn't clutter the to-do list.

Output has two sections:
  ALREADY HANDLED - the current matcher now resolves this (bare nickname,
                    filler-word/casing normalization, etc.). It'll stop
                    appearing in the log after the next restart. Nothing to do.
  STILL UNMATCHED - resolves in no engine. The genuine gap: a team/player not
                    in our data (e.g. ITF/challenger tennis -> needs a data
                    source, not an alias) or, occasionally, a real alias to add
                    once you CONFIRM the exact source name. Never guess.

Usage:
    python3 suggest_aliases.py [logfile]     # default: divergence_bot.log
"""

import difflib
import json
import logging
import re
import sys
from collections import Counter

import name_match
from elo.engine import EloEngine

logging.disable(logging.WARNING)  # silence resolve()'s own info/warning logs

LOG = sys.argv[1] if len(sys.argv) > 1 else "divergence_bot.log"
RATINGS = "elo_ratings.json"


def base_names(engine: EloEngine) -> list[str]:
    return [k for k in engine.ratings if "|" not in k]


def main():
    pat = re.compile(r"no Elo match for '([^']+)'")
    names: Counter = Counter()
    try:
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    names[m.group(1)] += 1
    except FileNotFoundError:
        print(f"Log not found: {LOG}")
        return
    if not names:
        print(f"No 'no Elo match' warnings in {LOG} — nothing to harvest.")
        return

    try:
        raw = json.load(open(RATINGS, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Could not load {RATINGS}: {e}")
        return
    engine_names = {k: base_names(EloEngine.from_dict(v)) for k, v in raw.items()}

    handled, still = [], []
    for name, count in names.most_common():
        hits = []
        for sport, keys in engine_names.items():
            r = name_match.resolve(name, keys)
            if r:
                hits.append((sport, r))
        (handled if hits else still).append((name, count, hits))

    print(f"{len(names)} distinct unmatched name(s) in {LOG}\n")

    print(f"=== ALREADY HANDLED by the current matcher ({len(handled)}) — "
          f"these clear from the log after a restart, nothing to do ===")
    for name, count, hits in handled:
        show = ", ".join(f"{s}:{r!r}" for s, r in hits[:3])
        print(f"  {name!r} (x{count}) -> {show}")

    print(f"\n=== STILL UNMATCHED ({len(still)}) — the genuine gaps ===")
    for name, count, _ in still:
        # show closest candidates to spot a real alias among the stuck ones
        best = []
        for sport, keys in engine_names.items():
            for cand in keys:
                sc = difflib.SequenceMatcher(None, name.lower(), cand.lower()).ratio()
                if sc >= 0.7:
                    best.append((sc, sport, cand))
        best.sort(reverse=True)
        tip = ", ".join(f"{s}:{c!r}({sc:.2f})" for sc, s, c in best[:3]) or "(nothing close — truly absent)"
        print(f"  {name!r} (x{count})")
        print(f"         {tip}")

    print("\nSTILL-UNMATCHED are usually players/teams not in our data (ITF/challenger "
          "tennis needs a data source, not an alias). Add to name_match.ALIASES only a "
          "CONFIRMED match, never a guess.")


if __name__ == "__main__":
    main()
