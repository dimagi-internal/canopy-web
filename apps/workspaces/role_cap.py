"""Acting as a LOWER role than you hold — a turn's `acts_as` (canopy-web#1705).

Whoever starts a turn may ask for it to run as an `editor` or a `viewer` (`TurnIn.acts_as`,
`LocalTurnIn.acts_as`): everything about that turn — whether it may be started, its
mode, its caller envelope, its credential reads — is judged as if they held only that
role. It is how anyone, an agent included, runs the non-admin path (licenses, the manual
cap, refusals) for real, without a second account, a second machine, or a setting left on.
The cap lives on the turn (`origin_ref["acts_as"]`), so it ends with the turn.

A client — a runner, a session, a script — can also put ITSELF in a lower mode, with
nothing stored anywhere: every request carrying `X-Canopy-Acts-As: editor|viewer` is
judged at that role, and a runner claiming with it stamps the cap on the turns of its
owner's it claims (`claim.claim_next_turn(acts_as=)`). How a runner started with
`CANOPY_ACTS_AS=editor` runs as an editor until it is restarted without it.

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
#: The caps a turn or a request may act as. Owner/admin are not caps: they are the default.
CAPS = (EDITOR, VIEWER)
#: The header a client puts itself in a lower mode with, request by request.
HEADER = "HTTP_X_CANOPY_ACTS_AS"


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


def stronger(a: str, b: str) -> str:
    """The stronger restriction of two caps: viewer over editor over none."""
    return VIEWER if VIEWER in (a, b) else (EDITOR if EDITOR in (a, b) else "")


def capped_role(role: str | None, cap: str) -> str | None:
    """`role` lowered to `cap` (a workspace role string), or `role` unchanged."""
    if role is None or cap not in CAPS:
        return role
    from .models import WorkspaceMembership

    rank = WorkspaceMembership.ROLE_RANK
    return cap if rank.get(role, -1) > rank[cap] else role


def apply_to_request(request) -> None:
    """Cap `request.user` by the mode the client put itself in (`X-Canopy-Acts-As`),
    and by the turn the request is made during (`X-Canopy-Turn-Id`) when they started
    it as a lower role — a session cannot shed that cap mid-turn. Only the turn's own
    initiator: a runner owner reading for someone else's capped turn is judged as
    themselves, as for any turn their box runs. Called by the bearer middleware."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return
    apply(user, header_cap(request))
    apply(user, _turn_cap(request, user))


def header_cap(request) -> str:
    """The mode the client put itself in for this request (`X-Canopy-Acts-As`)."""
    return cap_from(request.META.get(HEADER, ""))


def cap_from(value) -> str:
    """`value` as a cap ("editor" / "viewer"), or "" — what a turn's `acts_as` holds."""
    cap = str(value or "").strip().lower()
    return cap if cap in CAPS else ""


def _turn_cap(request, user) -> str:
    import uuid

    raw = request.META.get("HTTP_X_CANOPY_TURN_ID", "").strip()
    if not raw:
        return ""
    try:
        tid = uuid.UUID(raw)
    except ValueError:
        return ""
    from apps.harness.models import Turn

    ref = (Turn.objects.filter(pk=tid, initiator_user=user)
           .values_list("origin_ref", flat=True).first())
    return cap_from((ref or {}).get("acts_as"))
