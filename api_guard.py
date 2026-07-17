"""Cloudflare-ban detection and cooldown — the rate-limit circuit breaker.

On 2026-07-16 api.polymarket.us temporarily BANNED the droplet (Cloudflare
error 1015, "banned you temporarily") for ~3 hours. The dominant load was the
full market-catalog refetch (~90 paginated calls in a ~6 s burst, every 60 s
cycle); a busy match window stacked settlement/CLV calls on top and tipped it
over. The catalog cache in fetch_all_markets removed the burst; this module
handles the residual case: if the exchange ever answers with a ban page again,
STOP CALLING for a cooldown instead of hammering through the ban (which resets
Cloudflare's rolling window and extends it).

Pausing is safe by design — 2026-07-16 proved it live: for the whole 3 h ban
every check failed loudly and concluded nothing (RESOLUTION_CHECK_FAILED
sentinel, fail-open guards), and all ten affected positions settled and
reconciled correctly once the ban lifted. A deliberate pause is strictly
better than an involuntary one.

Only genuine BAN pages arm the cooldown. Cloudflare 5xx pages (origin down)
and ordinary API errors do not — those are transient and the per-call
fail-open handling already covers them; pausing trading over one flaky
response would cost entries for nothing.

Shared by divergence_bot.py and manual_sync.py. Imports nothing from either
(config only) — safe to import anywhere.
"""

from __future__ import annotations

import logging
import time

import config

log = logging.getLogger("divergence_bot.api_guard")

# Phrases from the actual 1015 ban page served on 2026-07-16. Deliberately
# specific: "cloudflare" alone also matches their 5xx pages, and "access
# denied" alone could match an auth failure.
_BAN_MARKERS = (
    "banned you temporarily",
    "used cloudflare to restrict access",
    "error 1015",
    "error code: 1015",
)

_ban_until = 0.0      # time.monotonic() deadline; 0 = not banned
_current_cooldown = 0.0  # the escalated cooldown (seconds) applied at the last ban


def note_error(err) -> bool:
    """Inspect a failed exchange call. If the response is a Cloudflare ban
    page, arm the cooldown and return True; any other failure returns False
    and changes nothing. Call this from except-blocks around exchange calls —
    it never raises.

    Repeated bans escalate the cooldown (base -> x2 -> x4 ... up to
    API_BAN_COOLDOWN_MAX_MIN): a re-ban within API_BAN_RECENT_MIN of the
    previous one means the base pause resumed into a still-hot rate limiter,
    so waiting the same 5 min again just loops (2026-07-17: 11 ban episodes
    in 2 h doing exactly that). A ban after a longer clean gap is treated as
    a fresh incident and starts back at the base cooldown."""
    global _ban_until, _current_cooldown
    text = str(err).lower()
    if not any(m in text for m in _BAN_MARKERS):
        return False
    now = time.monotonic()
    base = getattr(config, "API_BAN_COOLDOWN_MIN", 5) * 60
    cap = getattr(config, "API_BAN_COOLDOWN_MAX_MIN", 40) * 60
    recent = getattr(config, "API_BAN_RECENT_MIN", 15) * 60
    if now < _ban_until:
        # Another ban page while already paused (several calls can be in
        # flight when the first one trips) — same episode, don't escalate.
        return True
    # "Recent" is measured from the END of the previous cooldown, not from the
    # previous detection — a 20-min pause must not itself age the incident out
    # of the escalation window.
    if _ban_until and now - _ban_until < recent:
        cooldown = min(_current_cooldown * 2 if _current_cooldown else base, cap)
    else:
        cooldown = base
    _current_cooldown = cooldown
    _ban_until = now + cooldown
    log.warning(
        f"Cloudflare BAN detected (error 1015) — pausing exchange calls "
        f"{cooldown / 60:.0f} min so the ban can expire. Open positions are "
        f"safe: fills/settlements catch up when calls resume."
    )
    return True


def cooldown_remaining() -> float:
    """Seconds left in the ban cooldown; 0.0 when it's fine to call."""
    return max(0.0, _ban_until - time.monotonic())


def seconds_since_ban_end() -> float | None:
    """Seconds since the last ban cooldown ended, or None if no ban has been
    seen this run. 0.0 while a cooldown is still active. Lets heavy callers
    (the catalog refetch) hold back a grace period after a ban instead of
    slamming a still-warm rate limiter the moment calls resume."""
    if not _ban_until:
        return None
    return max(0.0, time.monotonic() - _ban_until)
