"""The Claude model this box's sessions start on, as its settings name it.

Reported on the heartbeat as `claude_model` so the chat can say how full a
session's context window is. The transcript cannot answer that alone: it records
the API model id (`claude-opus-5-5`) and never the `[1m]` that selects the
1M-token window, which lives only in the setting (`"model": "opus[1m]"`).

Read the way Claude Code itself resolves it, most specific first: the
`ANTHROPIC_MODEL` environment variable, then the account's `settings.json`. A
session switched with `/model` afterwards is not seen here — this is the box's
default, a hint the chat combines with what the transcript proves (a context
past 200k tokens can only be the 1M window). "" when nothing says.
"""
from __future__ import annotations

import json
import os
import time

from . import hooks

#: Settings are re-read at most this often: the heartbeat runs every few seconds.
TTL_SECONDS = 60
MAX_LEN = 64

_cache: tuple[float, str] | None = None


def configured(*, now: float | None = None) -> str:
    """The model setting, trimmed to MAX_LEN, or "" when none is set or readable."""
    global _cache
    t = time.monotonic() if now is None else now
    if _cache is not None and t - _cache[0] < TTL_SECONDS:
        return _cache[1]
    value = _read()
    _cache = (t, value)
    return value


def _read() -> str:
    env = (os.environ.get("ANTHROPIC_MODEL") or "").strip()
    if env:
        return env[:MAX_LEN]
    try:
        data = json.loads(hooks.user_settings_path().read_text())
    except (OSError, ValueError):
        return ""
    model = data.get("model") if isinstance(data, dict) else None
    return model.strip()[:MAX_LEN] if isinstance(model, str) else ""


def reset() -> None:
    """Forget the cached value (tests)."""
    global _cache
    _cache = None
