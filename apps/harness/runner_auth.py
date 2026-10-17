"""What a runner's own credential (`RunnerAccessToken`) may reach.

A runner token authenticates as the runner's owner — every runner gate is
owner-scoped — so the narrowing is here, by path, before any view runs. It is the
runner protocol and the few reads a box makes to prepare its agents, and nothing
else: a box cannot use it to read a workspace's content, change a setting, or act
in a chat as its owner.

Paths are matched FLAT (`device_keys._strip_to_flat`): no legacy `/canopy`
prefix, no `/api/w/<ws>` segment, no trailing slash.
"""
from __future__ import annotations

import re

from .device_keys import _strip_to_flat

_SLUG = r"[a-z0-9][a-z0-9_-]*"

#: (methods, pattern). `{runner}` is replaced by the token's own runner id, so a
#: box can reach its OWN runner routes and never another's.
_RULES: tuple[tuple[frozenset[str], str], ...] = (
    # The runner protocol: heartbeats, claims, streams, mint steps, sessions …
    (frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"}), r"/api/harness/runners/{runner}(/.*)?"),
    # Reporting a turn it holds (start/events/finish) and enqueueing an inbound
    # one. The views check the turn is this owner's or this box's ("runner").
    (frozenset({"GET", "POST"}), r"/api/harness/turns(/.*)?"),
    # Preparing its agents: the roster, one agent, its credentials.
    (frozenset({"GET"}), r"/api/agents"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/credentials/resolve"),
    (frozenset({"GET"}), r"/api/workspaces"),
    # Its device key: re-registering after an ECDH key change.
    (frozenset({"GET", "POST"}), r"/api/runner-device-keys"),
    # Telemetry it reports (inbox and drill events).
    (frozenset({"POST"}), r"/api/events"),
)


def reaches(method: str, path: str, runner_id) -> bool:
    """May a runner token for `runner_id` make this request?"""
    flat = _strip_to_flat(path)
    rid = re.escape(str(runner_id))
    for methods, pattern in _RULES:
        if method.upper() in methods and re.fullmatch(pattern.replace("{runner}", rid), flat):
            return True
    return False


def reaches_ws(path: str, runner_id) -> bool:
    """A runner token opens its own runner's control channel and nothing else."""
    return bool(re.fullmatch(rf"/?ws/runner/{re.escape(str(runner_id))}/?", path or ""))

