"""Reconciles Polymarket's team/player name strings against the names used
by each sport's Elo data source. Exact matches cover most of MLB/NBA/WNBA
team names and World Cup country names, but formatting can still drift
(abbreviations, accents, suffixes) — especially for tennis player names.

This is the single messiest, highest-maintenance part of the whole system.
Approach, in order: exact match, then a small seed alias table (extend this
as mismatches show up), then fuzzy matching (stdlib difflib) as a last
resort. Every fuzzy match and every outright miss is logged so gaps are
visible rather than silently trading on a wrong pairing — a market with an
unresolved name is skipped, never guessed.
"""

import difflib
import logging
import re

log = logging.getLogger("divergence_bot.name_match")


def _tokens(name: str) -> frozenset[str]:
    """Lowercased word/number tokens of a name, e.g. 'Dallas Wings' ->
    {'dallas','wings'}. Punctuation/spacing ignored."""
    return frozenset(re.findall(r"[a-z0-9]+", (name or "").lower()))


# Generic org-type words that don't distinguish one team from another —
# Polymarket appends these where our esports sources omit them (and vice
# versa): "LPH Gaming" == "LPH", "BetBoom Team" == "BetBoom", "The Huns
# Esports" == "The Huns". Deliberately excludes qualifiers that DO
# distinguish squads, e.g. "Academy" (LYON Academy != LYON) or "Sector".
_ORG_FILLER = {"gaming", "team", "esports", "esport", "clan", "club"}


def _norm(name: str) -> str:
    """Case-folded core of a name with org-filler words removed, for a
    stricter-than-fuzzy equality: 'summer bear' -> 'summer bear',
    'LPH Gaming' -> 'lph'. Falls back to the full token string if stripping
    filler would empty it (a team literally named 'Team')."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [t for t in toks if t not in _ORG_FILLER]
    return " ".join(core or toks)

# Seed aliases: Polymarket name -> Elo-source name. Extend this as unmatched
# names show up in divergence_bot.log.
ALIASES: dict[str, str] = {
    # "Polymarket Team Name": "Elo Source Name",
    # Polymarket lists the 2026 WNBA expansion teams as bare city names with
    # an empty alias field (unlike every other WNBA team) — Toronto seen live
    # 2026-07-07, Portland seen live 2026-07-10. Each resolves only against the
    # WNBA engine (its full name isn't in any other sport's ratings), so the
    # bare-city alias can't mis-map an NBA "Portland" market.
    "Toronto": "Toronto Tempo",
    "Portland": "Portland Fire",
    # OpenDota's team directory names BetBoom "BB Team" (verified via
    # /api/teams 2026-07-08). Polymarket lists them as "BetBoom Team".
    "BetBoom Team": "BB Team",
}

FUZZY_CUTOFF = 0.85

# Names already warned about this run. A market stays in the scan for hours,
# so an unmatched name otherwise re-warns EVERY cycle — one ITF weekend put
# 870 duplicate warnings in the ops digest (2026-07-13) and drowned the
# signal. First miss warns; repeats log at DEBUG. (A ratings hot-reload can
# make a name resolvable later — then resolve() simply succeeds and the
# stale entry here is harmless.)
_WARNED_UNMATCHED: set[str] = set()


def resolve(polymarket_name: str, known_names) -> str | None:
    """Maps a Polymarket team/player name to the matching name key used in
    an EloEngine's ratings. Returns None (never guesses past the confidence
    cutoff) if nothing resolves."""
    if not polymarket_name:
        return None
    known = list(known_names)
    if polymarket_name in known:
        return polymarket_name
    aliased = ALIASES.get(polymarket_name)
    if aliased and aliased in known:
        return aliased
    # Case- and filler-insensitive exact match: handles pure casing
    # differences ("summer bear" == "Summer Bear") and appended org words
    # ("LPH Gaming" == "LPH", "BetBoom Team" == "BetBoom"). Requires a UNIQUE
    # normalized hit within this sport so it can never mis-map.
    nq = _norm(polymarket_name)
    if nq:
        norm_hits = [n for n in known if _norm(n) == nq]
        if len(norm_hits) == 1:
            if norm_hits[0] != polymarket_name:
                log.info(f"norm-matched {polymarket_name!r} -> {norm_hits[0]!r}")
            return norm_hits[0]
    # Bare-token match: Polymarket sometimes sends only a nickname ("Warriors")
    # or only a city ("Dallas") while our ratings hold the full "City Nickname".
    # If the query's tokens are a subset of EXACTLY ONE rated name in this
    # sport, that's an unambiguous identification. Scoped per-engine, so
    # "Dallas" -> Wings in a WNBA market but Mavericks in an NBA one; and a
    # city with two teams in the same league ("Chicago" in MLB = White Sox AND
    # Cubs) stays ambiguous and is skipped, never mis-mapped. Requires 2+
    # tokens on the RATED side too, so a bare query can't "identify" a
    # single-token rating that merely contains it.
    q = _tokens(polymarket_name)
    if q:
        supersets = [n for n in known if len(_tokens(n)) > len(q) and q <= _tokens(n)]
        if len(supersets) == 1:
            log.info(f"token-matched {polymarket_name!r} -> {supersets[0]!r}")
            return supersets[0]
    matches = difflib.get_close_matches(polymarket_name, known, n=1, cutoff=FUZZY_CUTOFF)
    if matches:
        log.info(f"fuzzy-matched {polymarket_name!r} -> {matches[0]!r} — verify and add to ALIASES if wrong")
        return matches[0]
    if polymarket_name in _WARNED_UNMATCHED:
        log.debug(f"no Elo match for {polymarket_name!r} (repeat)")
    else:
        _WARNED_UNMATCHED.add(polymarket_name)
        log.warning(f"no Elo match for {polymarket_name!r} — add to name_match.ALIASES once you find the source name")
    return None
