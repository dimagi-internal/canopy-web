"""Kind: let an agent act as you on canopy-web for 30 minutes (an act-as-you permission).

Filed only by `POST /api/me/act-as-requests/` (`apps/approvals/api_act_as.py`) with the agent's
turn login: its one param is the server-made `ActAsGrant`, whose person canopy-web worked out from
the turn (`harness.turn_asker`), never from anything the agent sent. It is PERSONAL: addressed to
that one person, who may not be a member of the agent's workspace, and seen and decided by nobody
else. Approving needs a passkey touch made for this request (`Kind.step_up`), and the person picks
read-only or read + write (`choices`; write only when the agent asked for it and the turn may hold
it, `act_as.unreviewed_write_refusal`). Everything about the permission itself (its 30 minutes, the
turn or conversation it dies with, the token returned once, what it never reaches) is
`apps/harness/act_as.py`; this kind is only how the person says yes or no.
"""
from __future__ import annotations

import uuid

from apps.common.schemas import StrictModel
from apps.harness import act_as
from apps.harness.models import ActAsGrant

from . import Described, ExecError, Kind, KindError, register

SLUG = "agent.act_as"

_ASKED = {ActAsGrant.READ: "read-only", ActAsGrant.WRITE: "read + write",
          ActAsGrant.EITHER: "read-only or read + write (your choice)"}
_LABEL = {ActAsGrant.READ: "Allow read-only (30 min)", ActAsGrant.WRITE: "Allow read + write (30 min)"}


class Params(StrictModel):
    request: uuid.UUID


def _grant(pk) -> ActAsGrant:
    grant = (ActAsGrant.objects.select_related("person", "agent", "agent__workspace", "turn")
             .filter(pk=pk).first())
    if grant is None:
        raise KindError(f"there is no act-as-you request {pk}")
    return grant


def idempotency_key(grant: ActAsGrant) -> str:
    """The one approval a grant files; `execute` checks the approval it runs for is that one."""
    return f"{SLUG}:{grant.pk}"


@register
class AgentActAs(Kind):
    slug = SLUG
    label = "Let an agent act as you on canopy"
    params_model = Params
    needs = "the person it would act as, with a passkey touch made for this request"
    does = ("Lets the agent use canopy as you, within your own access, for 30 minutes from your passkey touch "
            "and only while its turn or conversation lasts; read-only or read + write, as you choose. It never "
            "reaches your tokens, passkeys, vaults, memory settings or these permissions.")
    step_up = True
    personal = True

    def describe(self, p: Params) -> Described:
        grant = _grant(p.request)
        agent = grant.agent
        name = agent.name or agent.slug
        return Described(
            workspace=agent.workspace_id,
            title=f"Let {name} act as you on canopy",
            summary=f"{name} asks to use canopy as you ({_ASKED.get(grant.requested_scope, '')}) for 30 minutes.",
            plan=[f"{name} may use canopy as you, within your own access: {_ASKED.get(grant.requested_scope, '')}",
                  "For 30 minutes from your passkey touch, never extended, and only while its turn or "
                  "conversation with you lasts",
                  "It can never touch your tokens, passkeys, vaults, memory settings or these permissions",
                  "Everything it does is recorded as " + act_as.attribution(grant),
                  "You can stop it at any time (Settings → Act as you)"],
            scopes=[f"agent:{agent.slug}"])

    def approvers(self, p: Params, d: Described) -> list:
        grant = _grant(p.request)
        return [grant.person] if grant.person.is_active else []

    def recipients(self, p: Params, d: Described) -> list:
        return self.approvers(p, d)

    def may_approve(self, user, p: Params, d: Described) -> bool:
        if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", True):
            return False
        try:
            return _grant(p.request).person_id == user.pk
        except KindError:
            return False

    def choices(self, p: Params) -> list[tuple[str, str]]:
        try:
            grant = _grant(p.request)
        except KindError:
            return []
        offered = [ActAsGrant.READ, ActAsGrant.WRITE] if grant.requested_scope == ActAsGrant.EITHER \
            else [grant.requested_scope]
        offered = [s for s in offered
                   if s == ActAsGrant.READ or act_as.unreviewed_write_refusal(grant.turn, s, person=grant.person) is None]
        return [(s, _LABEL[s]) for s in offered]

    def lapsed(self, p: Params) -> str | None:
        try:
            grant = _grant(p.request)
        except KindError as exc:
            return str(exc)
        status = act_as.status_of(grant)
        return None if status == ActAsGrant.PENDING else f"the request is {status}"

    def execute(self, user, p: Params, request, *, choice: str = "") -> dict:
        grant = _grant(p.request)
        if request.idempotency_key != idempotency_key(grant):
            raise ExecError("This approval is not the one filed for that act-as-you request.")
        try:
            grant = act_as.approve(grant, user, choice)
        except act_as.Refused as exc:
            raise ExecError(str(exc)) from exc
        return {"request": str(grant.pk), "agent": grant.agent.slug, "scope": grant.scope,
                "expires_at": grant.expires_at.isoformat()}

    def closed(self, p: Params, request) -> None:
        try:
            grant = _grant(p.request)
        except KindError:
            return
        if act_as.settle(grant).status != ActAsGrant.PENDING:
            return
        if request.status == request.DECLINED:
            act_as.decline(grant, grant.person)
        elif request.status == request.CANCELLED:
            act_as.stop(grant, by="person" if request.decided_by_id == grant.person_id else "agent")
        else:
            act_as.lapse(grant)
