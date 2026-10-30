"""A debugging aid for lifetimes: shorten a window to reproduce its end quickly."""
import logging
import os

log = logging.getLogger(__name__)


def debug_shortened(default: int, env_name: str) -> int:
    """``default``, or the shorter value ``env_name`` asks for.

    A debugging aid for reproducing what happens when a window ends (a chat sealed
    after the passkey window, a token that lapses) without waiting half an hour:
    set e.g. ``CANOPY_HOST_GRANT_CONFIRM_SECONDS=30`` on a dev or staging box. It can
    only SHORTEN a lifetime, never lengthen one, and not below 5 seconds, so no
    setting of it can widen what a passkey touch grants. A value that is not a
    number is ignored.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        asked = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number of seconds; ignored", env_name, raw)
        return default
    value = max(5, min(default, asked))
    log.warning("%s: lifetime shortened from %ss to %ss for debugging", env_name, default, value)
    return value
