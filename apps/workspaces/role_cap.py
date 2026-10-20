"""Acting as a LOWER role than you hold — the private runner's `acts_as` (canopy-web#1705).

A person can set their own private runner to act as an `editor` or a `viewer`, so that
everything from that box — its remote turns and the local sessions on it — is judged
as if they held only that role. It is how the agent's owner tests the non-admin path
(licenses, the manual cap, refusals) without a second account or a second machine.

The cap rides on the user OBJECT (`CAP_ATTR`), set once per request or per turn, and
the few readers every gate goes through honour it:

* `workspaces.services.member_role` / `member_roles` — the workspace role is lowered to
  the cap (never raised: a cap above what you hold changes nothing);
* `Agent.is_admin` — never an agent admin under any cap;
* `caller_context.relationship_for_user` — never the agent's OWNER under a cap, so
  `access.decide` places the person in the tier the cap names;
* `agents.services.caller_runs_agent` — a capped caller is not "a box that runs the
  agent"; its credential reads go through its license like anyone else's.

A cap only ever LOWERS what a caller may do, so it needs no proof of its own: whoever
can send a request as you can already do everything the uncapped request could.
"""
from __future__ import annotations

#: The attribute on a User instance that carries its cap for this request or turn.
CAP_ATTR = "_canopy_acts_as"

EDITOR, VIEWER = "editor", "viewer"
#: The caps a private runner may act as. Owner/admin are not caps: they are the default.
CAPS = (EDITOR, VIEWER)


def cap_of(user) -> str:
    """The user's cap ("editor" / "viewer"), or "" when they act as themselves."""
    return getattr(user, CAP_ATTR, "") or ""


def apply(user, cap: str):
    """Cap `user` at `cap` (in place; returns it). Only ever lowers: applying a cap
    weaker than the one already set keeps the stronger restriction."""
    if user is None or cap not in CAPS:
        return user
    current = cap_of(user)
    if current == VIEWER or (current == EDITOR and cap == EDITOR):
        return user
    setattr(user, CAP_ATTR, cap)
    return user


def capped_role(role: str | None, cap: str) -> str | None:
    """`role` lowered to `cap` (a workspace role string), or `role` unchanged."""
    if role is None or cap not in CAPS:
        return role
    from .models import WorkspaceMembership

    rank = WorkspaceMembership.ROLE_RANK
    return cap if rank.get(role, -1) > rank[cap] else role


def apply_to_request(request) -> None:
    """Cap `request.user` by the private runner the request speaks for, if it acts as
    a lower role: the runner a runner token belongs to, or the one named in
    `X-Canopy-Runner` when the caller owns it. Called by the bearer middleware."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return
    rtok = getattr(request, "runner_token", None)
    runner = rtok.runner if rtok is not None else _named_runner(request, user)
    if runner is not None and getattr(runner, "private", False) and runner.acts_as:
        apply(user, runner.acts_as)
        request.acts_as_runner = runner
    if rtok is None:
        # A read made during the caller's own local turn keeps the cap the turn was
        # opened under, header or not — a session cannot shed it mid-turn.
        apply(user, _local_turn_cap(request, user))


def _local_turn_cap(request, user) -> str:
    import uuid

    raw = request.META.get("HTTP_X_CANOPY_TURN_ID", "").strip()
    if not raw:
        return ""
    try:
        tid = uuid.UUID(raw)
    except ValueError:
        return ""
    from apps.harness.models import Turn

    ref = (Turn.objects.filter(pk=tid, origin=Turn.ORIGIN_LOCAL, initiator_user=user)
           .values_list("origin_ref", flat=True).first())
    return (ref or {}).get("acts_as") or ""


def _named_runner(request, user):
    import uuid

    raw = request.META.get("HTTP_X_CANOPY_RUNNER", "").strip()
    if not raw:
        return None
    try:
        rid = uuid.UUID(raw)
    except ValueError:
        return None
    from apps.harness.models import Runner

    return Runner.objects.filter(pk=rid, owner=user).first()
