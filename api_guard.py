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

_ban_until = 0.0  # time.monotonic() deadline; 0 = not banned


def note_error(err) -> bool:
    """Inspect a failed exchange call. If the response is a Cloudflare ban
    page, arm the cooldown and return True; any other failure returns False
    and changes nothing. Call this from except-blocks around exchange calls —
    it never raises."""
    global _ban_until
    text = str(err).lower()
    if not any(m in text for m in _BAN_MARKERS):
        return False
    cooldown = getattr(config, "API_BAN_COOLDOWN_MIN", 5) * 60
    deadline = time.monotonic() + cooldown
    if deadline > _ban_until:
        _ban_until = deadline
        log.warning(
            f"Cloudflare BAN detected (error 1015) — pausing exchange calls "
            f"{cooldown / 60:.0f} min so the ban can expire. Open positions are "
            f"safe: fills/settlements catch up when calls resume."
        )
    return True


def cooldown_remaining() -> float:
    """Seconds left in the ban cooldown; 0.0 when it's fine to call."""
    return max(0.0, _ban_until - time.monotonic())
