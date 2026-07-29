"""Log a best-effort failure loudly enough to notice, quietly enough to ignore.

Shared by every path that retries silently — the chat pump's transcript flush
and the live-stream/backfill posts. A single failure there is usually a blip;
one that REPEATS means stuck, not flaky, and that is the interesting kind.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("canopy_runner")

_failures: dict[str, int] = {}

# Warn on the first failure, then every Nth, so a permanently-stuck session stays
# visible in the log without drowning it (~5 min apart at the default tick).
_REWARN_EVERY = 60


def note_failure(key: str, what: str) -> None:
    """Count a best-effort failure and log it at a level someone will see."""
    n = _failures.get(key, 0) + 1
    _failures[key] = n
    if n == 1 or n % _REWARN_EVERY == 0:
        logger.warning("%s failed (attempt %d, still retrying): %s", what, n, key,
                       exc_info=True)
    else:
        logger.debug("%s failed (attempt %d): %s", what, n, key, exc_info=True)


def note_success(key: str) -> None:
    """Clear a failure streak; log the recovery if there was one to clear."""
    n = _failures.pop(key, 0)
    if n:
        logger.info("recovered after %d failed attempts: %s", n, key)



