"""Error classification + backoff shared by every fetch path.

The whole "100% & validated" guarantee rests on telling apart:
  * PERMANENT failures (404/410/300 ...): the URL is dead, record it and stop
    retrying so the queue can actually drain to empty.
  * TRANSIENT failures (timeouts, resets, 5xx, WAF blocks, OS errors): the page
    is fine, the network/site is having a moment — requeue with backoff and let
    the health monitor decide when to suspend.
"""
from __future__ import annotations

import re
from urllib.error import HTTPError, URLError

TRANSIENT = "transient"
PERMANENT = "permanent"

# HTTP status codes treated as permanently dead (no amount of retrying helps).
PERMANENT_HTTP = {300, 400, 404, 405, 410, 451}

_HTTP_IN_TEXT = re.compile(r"HTTP(?: Error)?\s+(\d{3})", re.I)


def _status_from(exc: BaseException) -> int | None:
    if isinstance(exc, HTTPError):
        return int(exc.code)
    m = _HTTP_IN_TEXT.search(str(exc))
    return int(m.group(1)) if m else None


def classify(exc: BaseException) -> str:
    """Return TRANSIENT or PERMANENT for a fetch exception."""
    status = _status_from(exc)
    if status is not None:
        if status in PERMANENT_HTTP:
            return PERMANENT
        if 500 <= status <= 599:
            return TRANSIENT
        if status in (403, 429):
            # WAF / rate-limit: back off, do not give up.
            return TRANSIENT
        # Other 4xx: treat as permanent (won't fix itself).
        if 400 <= status <= 499:
            return PERMANENT
    # No HTTP status -> network/transport problem -> transient.
    if isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)):
        return TRANSIENT
    return TRANSIENT


def backoff_seconds(attempt: int, *, base: float = 0.5, cap: float = 30.0,
                    jitter: float = 0.25) -> float:
    """Exponential backoff with a small deterministic jitter (no RNG so runs
    stay reproducible). `attempt` is 1-based."""
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    # jitter derived from attempt so it varies without Math.random-style nondet.
    return delay * (1.0 + jitter * ((attempt % 5) / 5.0))
