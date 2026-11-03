"""What flipping an agent's Slack switch does to the Slack app's commands.

`agents` is below `slack`, so it cannot call it. `apps/slack/apps.py` registers the
one function that can, and `set_slack_enabled` asks for it here; with Slack absent
the answer is the same one an unconnected workspace gets.
"""
from __future__ import annotations

from collections.abc import Callable

#: `(agent) -> (status, detail)`: add or remove the agent's `/<slug>` command in the
#: workspace's Slack app. `status` is "synced" / "not_configured" / "error".
Sync = Callable[[object], tuple[str, str]]

_sync: Sync | None = None


def register(sync: Sync) -> None:
    global _sync
    _sync = sync


def sync(agent) -> tuple[str, str]:
    if _sync is None:
        return "not_configured", "Slack is not connected to this workspace."
    return _sync(agent)
