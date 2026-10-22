"""Every REST route, and the gate it enforces — declared, so a new one cannot arrive without one.

WHY THIS EXISTS. The ACL audit of 2026-10-02 did not find one bad gate; it found
the same mistake made route by route. Product surfaces let a `viewer` approve a
DDD gate and wipe a whole feed with `{}`; schedules and turns let a viewer
write a prompt and fire it at the fleet; six tenancy predicates had each grown a
NULL-means-allow leg. Each was a route whose author never wrote down who may
call it — the default was simply whatever the helper they reached for happened
to check, and "any member" is the cheapest helper there is. And since
2026-09-29 every route is also an MCP tool, so a route's gate is the gate on
every client holding a token.

So the rule is the one a reviewer cannot skip: **a new route fails CI until it
declares its gate, at the route, and a write a VIEWER can make must also say
why.** The point is not the declaration — it is that the question gets asked at
the moment the route is written, by the person writing it, and the answer is
visible in the diff, beside the code it describes::

    @router.patch("/{slug}/turn-mode", ...)
    @gate("agent.work", setting=True)
    def set_turn_mode(request, slug: str, payload: TurnModeIn): ...

``@gate`` goes directly above the ``def`` (so any wrapper made with
``functools.wraps`` carries it) and only marks the function. Its arguments:

* the labels (one or more, from the vocabulary below);
* for a write (POST/PUT/PATCH/DELETE), exactly one of ``setting=True`` — it
  changes how something is configured (who may do what, where work runs, which
  credential or integration is used, what an agent or workspace is set to do),
  so the settings audit (``apps/events/audit.py``) records every call to it, from
  every entry point, as a ``SettingChange`` in the same transaction — or
  ``kind=`` one of ``WRITE_KINDS``, the kind of write it is instead. When unsure,
  it is a setting: an extra row costs nothing, a missing one is the gap the
  audit exists to close;
* ``viewer_write="<reason>"`` on a write whose labels include plain ``member``:
  a write a viewer can make is a decision, never a default.

What `tests/test_every_route_declares_its_gate.py` checks: every operationId in
the schema carries a gate; every label is in `VOCABULARY`; every member-level
write has its ``viewer_write`` reason and nothing else does; every write is a
setting or names its kind, and no read is either. What it cannot check is that
a label is TRUE — record what the view ENFORCES TODAY, not what it should,
having read the view and the helper it calls. A gate that looks wrong is
recorded as it is and fixed in the code; the declaration changes with the fix.

Several labels mean alternatives or conditions on one route (an agent turn
needs `agent.work`, a project turn only membership; a walkthrough is changed by
its uploader while an editor, or by a workspace owner). The route's own code is
the authority on how they combine; a short comment beside ``@gate`` says so
where it is not obvious.

Vocabulary (closed — add to it deliberately, never in passing):

* ``anonymous`` — no auth at all (``auth=None``) and no token.
* ``token-link`` — anonymous allowed with a share/capability token (``?t=``),
  else member.
* ``authenticated`` — any signed-in user, not tenant-scoped (``/me``, requestable).
* ``self`` — signed in, and touches only the caller's OWN rows (PATs, push
  subscriptions, presence preference, shared transcripts).
* ``member`` — workspace membership (``permissions.READ``): a viewer passes.
* the capabilities of ``apps/workspaces/permissions.py``: ``content.write``,
  ``agent.work``, ``session.drive``, ``events.write``, ``logs.read``,
  ``members.manage``, ``integrations``, ``runners.route``, ``retention.manage``,
  ``admission.manage``, ``projects.decide``, ``own``.
* ``agent-admin`` — ``Agent.is_admin`` (its owner, a workspace owner, an
  ``AgentAdmin``).
* ``agent-owner`` — the agent's owner or a workspace owner (transfer, admin
  grants).
* ``agent-login`` — the agent's OWN canopy login (``Agent.user``), i.e. the
  agent calling with its own PAT, whatever its workspace role.
* ``session-acl`` — ``apps/canopy_sessions/access.py`` (read/write/share per chat).
* ``turn-content`` — ``apps/harness/turn_access.can_read_turn_content``.
* ``runner`` — the runner protocol: the runner's owner, or the box
  that claimed the turn.
* ``runner-admin`` — ``can_administer_runner``.
* ``runner-holds-agent`` — ``services.caller_runs_agent`` /
  ``runner_may_hold_agent``.
* ``license`` — an ACTIVE ``AgentLicense`` (``apps/agents/licenses.py``) and the
  caller's own live turn on that agent named in ``X-Canopy-Turn-Id``
  (``licenses.credential_principal``).
* ``contact`` — the ``/api/contact/`` surface, for contact principals.
* ``device-proof`` — no session or token: a proof from an ACTIVE runner device key
  (DPoP, or an EC2 instance role's signed STS call) bound to this request is the
  authority (``apps/harness/api_runner_sessions.py``).
* ``owner-token-and-key`` — no session: a token the runner's OWNER minted (revoked
  within 30 days is accepted) plus a proof of the key being registered; it only ever
  creates a PENDING key (``apps/harness/api_device_key_requests.py``).
* ``invited-role`` — no session or token: an EC2 instance role's signed STS call
  bound to this request, matched to a pairing invitation its creator APPROVED
  behind a passkey (or to that role's existing, approved runner of the same name)
  (``apps/harness/api_runner_invitations.py``).
* ``paired-key`` — no session or token: a DPoP proof by the box's own device key,
  bound to this request; opening a pairing makes nothing but the pairing, and
  claiming one also needs the one-time secret a person's passkey-gated approval
  handed to that box's loopback (``apps/harness/api_runner_pairings.py``).
* ``signed-link`` — a signed token in the URL is the authority (drill report,
  invite token, OAuth state).
* ``beta-reviewer`` — ``apps/beta_requests/services.may_review``: a superuser or
  the ``CANOPY_BETA_REQUESTS_TO`` address (a beta request names no workspace).
* ``superuser`` — ``User.is_superuser``: fleet-wide switches that span every tenant
  (the fleet hold).
* ``fleet-holder`` — an address in ``CANOPY_FLEET_HOLDERS`` (Ada): may SET the fleet
  hold, never release it (``apps/harness/api_runners.py::_may_hold_fleet``).
* ``host`` — a connected-site / machine protocol endpoint (assertion,
  jwt-bearer, Pub/Sub push).
* ``hcp-grant`` — the Human Context Protocol subject rule
  (``apps/contacts/hcp_api.py::_principal``): an agent reaches ONLY the person
  who started the turn it names, and only under that client's (agent, channel,
  host) grant, which the person can revoke. An OAuth client of the HCP service
  (``apps/contacts/hcp_oauth.py``) is held the same way: its ``hcpat_`` access
  token reaches ONLY the person whose grant it was issued under, within that
  grant's scopes and sources, and opens ``/api/hcp/v1/`` and nothing else.
* ``page-viewer`` — an agent's OWN login that a person let see their memory page
  (``apps/contacts/viewers.py::page_of``): reads that person's page and nothing
  else; never a caller token or an HCP client. Anyone else is 404.
* ``secret-request`` — ``apps/agents/secret_requests.may_answer``: someone who may
  answer an agent's OPEN request for one of its declared secrets — an agent admin,
  or a person who can write to the chat it was asked from (filling a MISSING value
  only); never an agent's own login. Seals ciphertext; never reads.
* ``caller-token`` — a confined turn's ``cct_`` caller token
  (``apps/harness/caller_tokens.py``): the turn is read from the token, never an
  argument. Used only by ``HAND_TOOL_GATES``.
* ``host-grant`` — a person's own DPoP-bound host-grant token, the only credential
  an MCP Apps View's button call carries (``apps/mcp/delegation.py``). Used only by
  ``HAND_TOOL_GATES``.

``HAND_TOOL_GATES`` (bottom of this file) is the same declaration for the MCP tools
that are not routes (``apps/mcp/tools/``): every hand-written tool names its gate
and whether it writes, and ``tests/test_every_route_declares_its_gate.py`` fails a
tool that is missing or stale. A hand-written tool that is a plain write should be
a route instead — it then gets the settings audit and the acting cap for free.
"""
from __future__ import annotations

from dataclasses import dataclass

VOCABULARY: frozenset[str] = frozenset({
    "anonymous", "token-link", "authenticated", "self", "member",
    "content.write", "agent.work", "session.drive", "events.write", "logs.read",
    "members.manage", "integrations", "runners.route", "retention.manage",
    "admission.manage", "projects.decide", "own",
    "agent-admin", "agent-owner", "agent-login", "session-acl", "turn-content",
    "runner", "runner-admin", "runner-holds-agent", "license",
    "contact", "signed-link", "host", "beta-reviewer", "superuser", "fleet-holder",
    "hcp-grant", "device-proof", "owner-token-and-key", "invited-role", "paired-key", "page-viewer",
    "secret-request", "caller-token", "host-grant",
})


@dataclass(frozen=True)
class Gate:
    """What one route declares: who may call it, and what kind of write it is."""

    labels: tuple[str, ...]
    setting: bool = False
    kind: str | None = None
    viewer_write: str | None = None


GATE_ATTR = "_canopy_gate"


def gate(*labels: str, setting: bool = False, kind: str | None = None, viewer_write: str | None = None):
    """Declare a route's gate on its view: ``@gate("agent.work", setting=True)``.

    ``labels`` are from VOCABULARY. A write is either ``setting=True`` (the
    settings audit records every call) or names its ``kind`` from WRITE_KINDS.
    A write a plain ``member`` (a viewer) can make carries ``viewer_write=`` with
    the reason. Put it directly above the ``def``; it only marks the function.
    """
    unknown = [label for label in labels if label not in VOCABULARY]
    if not labels or unknown:
        raise ValueError(f"@gate needs labels from route_gates.VOCABULARY, got {labels!r}")
    if setting and kind is not None:
        raise ValueError("@gate: a write is a setting or names its kind, not both")
    if kind is not None and kind not in WRITE_KINDS:
        raise ValueError(f"@gate: kind {kind!r} is not in route_gates.WRITE_KINDS")
    if viewer_write is not None and not viewer_write.strip():
        raise ValueError("@gate: viewer_write needs its reason")
    declared = Gate(tuple(labels), setting, kind, viewer_write)

    def mark(view):
        setattr(view, GATE_ATTR, declared)
        return view

    return mark


def gate_of(view) -> Gate | None:
    """The gate a view declared (wrappers made with ``functools.wraps`` carry it)."""
    return getattr(view, GATE_ATTR, None)


#: What a write that is NOT a setting is instead. Closed, like VOCABULARY.
WRITE_KINDS: dict[str, str] = {
    "content": "work product: docs, tasks, projects, reviews, narratives, runs, storyboards, issues",
    "conversation": "a chat or session: messages, attachments, lifecycle, page state",
    "turn": "dispatching or reporting a turn of work",
    "runner-protocol": "a runner daemon reporting in (heartbeats, claims, streams, mint steps)",
    "action": "a one-shot operation that changes no configuration (check, test, preview, run now, sync)",
    "person-data": "facts and records about a person, kept and retractable under their own rules",
    "telemetry": "events and reports recorded for later reading",
    "request": "asking for something someone else decides (access, beta)",
    "ephemeral-credential": "a short-lived ticket minted for one connection",
}


#: The MCP tools that are NOT routes (`apps/mcp/tools/`, registered with `@mcp.tool`):
#: name -> (gate labels from VOCABULARY, "read" | a WRITE_KINDS kind). A route
#: declares itself with @gate; a tool that bypasses the routes declares itself
#: here, so the question "who may call this" is asked for it too.
HAND_TOOL_GATES: dict[str, tuple[tuple[str, ...], str]] = {
    # The caller envelope of a turn: a member of the turn's tenant, or the
    # turn's own caller token. Same gate as `get_turn_caller_context`.
    "who_is_asking": (("member", "caller-token"), "read"),
    # The Connected site the visitor is on: caller tokens only, and only with
    # the visitor's live host grant (`apps/host_grants/host_gateway.py`).
    "site_tools": (("caller-token",), "read"),
    "site_call": (("caller-token",), "action"),
    # The caller's own open pages (or, for a caller token, its own conversation's).
    "current_page": (("self", "caller-token"), "read"),
    # /ide close review: carries an MCP Apps View, which has no REST shape.
    "propose_session_closes": (("session-acl",), "read"),
    # The View's Close button: a person's host grant only, then the session ACL.
    "close_proposed_sessions": (("host-grant", "session-acl"), "conversation"),
}
