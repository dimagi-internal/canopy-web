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
    # The owner's runner list — the updater's and the rebase check's one read.
    (frozenset({"GET"}), r"/api/harness/runners"),
    # Schedules: the laptop runner lists the ones due on it and fires a slot.
    (frozenset({"GET"}), r"/api/harness/schedules"),
    (frozenset({"POST"}), r"/api/harness/schedules/[^/]+/fire"),
    # Its agents' mailboxes: which to watch, and the watch it armed (or why not).
    (frozenset({"GET"}), r"/api/inbound/runner-mailboxes"),
    (frozenset({"POST"}), r"/api/inbound/watch"),
    # Preparing its agents: the roster, one agent, its credentials.
    (frozenset({"GET"}), r"/api/agents"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/credentials/resolve"),
    # … and the two reads that tell `canopy agent prepare` what it is resolving:
    # where the agent's repo lives, and which backend its secrets come from. A
    # runner token answers both only for agents its own runner serves
    # (`apps/agents/credential_resolve.credential_access`).
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/runtime"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/credentials/access"),
    # … and what it then reports back: the readiness `canopy agent prepare --runner`
    # posts after preparing one agent. Only for an agent this box serves
    # (`apps/agents/api.post_bootstrap_report`). Missing here, every report a
    # keyless box made was refused — 401 on the revoked PAT, 403 on its own token.
    (frozenset({"POST"}), rf"/api/agents/{_SLUG}/bootstrap-report"),
    (frozenset({"GET"}), r"/api/workspaces"),
    # Its device key: re-registering after an ECDH key change.
    (frozenset({"GET", "POST"}), r"/api/runner-device-keys"),
    # The bundled updater: the deployed version's update for its platform
    # (apps/harness/api_runner_releases.py). Not `downloads`: that is for people.
    (frozenset({"GET"}), r"/api/harness/runner-releases/current"),
    # Telemetry it reports (inbox and drill events).
    (frozenset({"POST"}), r"/api/events"),
)


#: What a PRIVATE runner's token reaches beyond the protocol (canopy-web#1705): its
#: owner's own Claude Code sessions on that machine sign in with the box's device key
#: instead of a PAT, so they open, renew and close their local turns and read their
#: agent's secrets under the owner's license (credentials/resolve, above). Only on a
#: private runner — the owner's own machine — never on
#: a shared box, whose token speaks for the fleet, not a person. Not
#: `/api/private-runners`: registering or relinking one is the person's, with their
#: own token, and a box's token has no reason to.
_PRIVATE_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"POST"}), r"/api/harness/local-turns(/.*)?"),
)


#: ALL a COMPUTER's token reaches (`Runner.COMPUTER`: a person's computer used in
#: plain Claude Code, no runner app). Not the runner protocol — it has no program to
#: heartbeat or claim — only what its owner's sessions there do: open, renew and
#: report their local turns (`api._reporting_turn_or_404` holds it to local turns
#: opened on this computer), and read an agent's secrets under the owner's license
#: during one (`apps/agents/credential_resolve.credential_access` never makes it a `runner`).
_COMPUTER_RULES: tuple[tuple[frozenset[str], str], ...] = _PRIVATE_RULES + (
    (frozenset({"POST"}), r"/api/harness/turns/[^/]+/(events|transcript|finish)"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/credentials/resolve"),
    (frozenset({"GET"}), rf"/api/agents/{_SLUG}/credentials/access"),
)


def reaches(method: str, path: str, runner_id, *, private: bool = False,
            computer: bool = False) -> bool:
    """May a runner token for `runner_id` make this request? `private`: the runner is
    its owner's private runner, which also opens its owner's local sessions.
    `computer`: the runner is a computer with no runner app — only its local
    sessions and their license reads (`_COMPUTER_RULES`), never the protocol."""
    flat = _strip_to_flat(path)
    rid = re.escape(str(runner_id))
    if computer:
        rules = _COMPUTER_RULES
    else:
        rules = _RULES + (_PRIVATE_RULES if private else ())
    for methods, pattern in rules:
        if method.upper() in methods and re.fullmatch(pattern.replace("{runner}", rid), flat):
            return True
    return False


def reaches_ws(path: str, runner_id, *, computer: bool = False) -> bool:
    """A runner token opens its own runner's control channel and nothing else — and
    a computer's (no runner app, no channel) opens none."""
    if computer:
        return False
    return bool(re.fullmatch(rf"/?ws/runner/{re.escape(str(runner_id))}/?", path or ""))

