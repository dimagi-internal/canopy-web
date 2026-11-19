"""`POST /api/coaching/facts/`: a coach says what it did, for analytics. Ids only.

The fact is born in the coach's own scripts (`docs/architecture/analytics.md`), so the
narrowness is here: a closed kind, closed words and ids, no text field to put a sentence
in, and only from inside a running, unconfined turn of the agent whose login calls. A
fact about a person also needs that person's live review grant, as far as this turn may
use it (`access.review_grantors`, the same rule as reading their sessions), so a turn
cannot file a fact about someone who never enrolled or someone its asker is not.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router, Schema, Status
from ninja.errors import HttpError
from pydantic import ConfigDict, model_validator

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.events import activity
from apps.harness import turn_asker
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.gates import membership_or_404
from apps.workspaces.gates import require as require_role
from apps.workspaces.models import WorkspaceMembership

from . import access, coaching

router = Router(auth=session_auth, tags=["coaching"])

_NOTE_REF = re.compile(r"^n-[0-9a-f]{8}$")
_FINDING_REF = re.compile(r"^f-[0-9a-f]{8}$")


class CoachingFactIn(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["coaching.note_sent", "coaching.reply", "coaching.finding_routed"]
    ref: str
    person_id: int | None = None
    note_type: Literal["retrospective", "explanatory", "status"] | None = None
    reply: Literal["useful", "wrong", "already_knew", "pause"] | None = None
    finding_type: Literal["knowledge-gap", "session-mistake", "system-defect"] | None = None

    @model_validator(mode="after")
    def _one_shape_per_kind(self):
        # A field another kind uses is refused, not ignored: `finding_routed` naming a
        # person must fail, since a friction pattern has no one in it.
        wants = {"coaching.note_sent": ("person_id", "note_type"),
                 "coaching.reply": ("person_id", "reply"),
                 "coaching.finding_routed": ("finding_type",)}[self.kind]
        for name in ("person_id", "note_type", "reply", "finding_type"):
            given = getattr(self, name) is not None
            if given != (name in wants):
                raise ValueError(f"{self.kind} {'needs' if name in wants else 'takes no'} {name}")
        shape = _FINDING_REF if self.kind == activity.COACHING_FINDING_ROUTED else _NOTE_REF
        if not shape.match(self.ref):
            raise ValueError(f"ref must match {shape.pattern}")
        return self


class CoachingFactOut(Schema):
    recorded: bool


@router.post("/facts/", response={200: CoachingFactOut, **refusals(422)}, summary="Record a coaching fact")
@gate("agent-login", kind="telemetry")  # the per-turn login of a running, unconfined turn; not a stored token
def record_coaching_fact(request: HttpRequest, payload: CoachingFactIn) -> dict:
    """For an agent's own login inside its running turn: a note went out, how a person
    replied, or a friction pattern was recorded. Recording the same fact again changes
    nothing (`recorded` is false). Each `kind` takes its own fields and no others
    (`note_sent`: person_id + note_type; `reply`: person_id + reply; `finding_routed`:
    finding_type) and its own `ref` shape; any other combination is a 422."""
    user = request.user
    turn = turn_asker.serving_turn(user)
    agent = turn_asker.turn_agent(turn) if turn is not None else None
    if agent is None or not agent.is_own_login(user):
        raise HttpError(403, "only an agent's own login, inside its running turn that is "
                             "not confined to a caller, may record a coaching fact")
    meta = {"turn": str(turn.pk)}
    ref = payload.ref
    if payload.kind == activity.COACHING_FINDING_ROUTED:
        meta["finding_type"] = payload.finding_type
    else:
        if not access.review_grantors(user).filter(user_id=payload.person_id).exists():
            raise HttpError(404, f"person {payload.person_id} not found")
        meta["person_id"] = payload.person_id
        if payload.kind == activity.COACHING_NOTE_SENT:
            meta["note_type"] = payload.note_type
        else:
            meta["reply"] = payload.reply
            ref = f"{ref}:{payload.reply}"  # one note can be answered more than one way
    fact = activity.emit(payload.kind, workspace=agent.workspace_id, actor=user, agent=agent,
                         surface="coaching", ref=ref, meta=meta, dedupe=True)
    return {"recorded": fact is not None}


# ---- who coaches: a workspace owner designates agents and people --------------------------------------

class CoachIn(Schema):
    """Owner-only. Name exactly one: an `agent` (its slug) or a person (their `email`, a member)."""

    model_config = ConfigDict(extra="forbid")

    agent: str = ""
    email: str = ""


class CoachOut(Schema):
    id: int
    kind: str  # "agent" | "person"
    slug: str = ""
    name: str
    workspace: str
    created_at: dt.datetime


def _coach_out(role) -> dict:
    return {"id": role.pk, "kind": "agent" if role.agent_id else "person",
            "slug": role.agent.slug if role.agent_id else "",
            "name": (role.agent.name or role.agent.slug) if role.agent_id
            else (role.user.get_full_name() or role.user.email or role.user.username),
            "workspace": role.workspace_id, "created_at": role.created_at}


@router.get("/workspaces/{slug}/coaches/", response={200: list[CoachOut], **refusals(404)},
            summary="Who coaches this workspace's people (the coaching program)")
@gate("member")
def list_coaches(request: HttpRequest, slug: str) -> list[dict]:
    """The agents and people designated to coach this workspace and everything below it: what a person
    agrees to when they opt into coaching. Any member may see who they are."""
    m = membership_or_404(request.user, slug)
    return [_coach_out(r) for r in coaching.roles_in(m.workspace)]


@router.post("/workspaces/{slug}/coaches/", response={200: CoachOut, **refusals(403, 404, 422)},
             summary="Designate a coach for this workspace and below (owner-only)")
@gate("own", setting=True)
def add_coach(request: HttpRequest, slug: str, payload: CoachIn) -> dict:
    """Owner-only. Name an `agent` (its slug; it must live in this workspace's tree) or a person (`email`;
    a member of it). The coach may then read people who opted into coaching and are in this workspace or
    below, read-only, never sensitive sessions (`canopy_sessions.access`, leg 5). Nobody is read until
    they opt in themselves. Designating the same coach again is a no-op."""
    m = require_role(request.user, slug, perms.OWN)
    ws = m.workspace
    agent_slug, email = payload.agent.strip(), payload.email.strip()
    if bool(agent_slug) == bool(email):
        raise HttpError(422, "name exactly one: an agent (its slug) or a person (their email)")
    tree = wsvc.with_ancestors({ws.slug}) | wsvc.descendant_slugs({ws.slug})
    if agent_slug:
        agent = Agent.objects.filter(slug=agent_slug, workspace_id__in=tree).first()
        if agent is None:
            raise HttpError(404, f"no agent {agent_slug!r} in this workspace's tree")
        role = coaching.designate(ws, agent=agent, by=request.user)
    else:
        person = get_user_model().objects.filter(email__iexact=email).first()
        if person is None or not WorkspaceMembership.objects.filter(user=person, workspace_id__in=tree).exists():
            raise HttpError(404, f"{email} is not a member of this workspace's tree")
        role = coaching.designate(ws, user=person, by=request.user)
    return _coach_out(role)


@router.delete("/workspaces/{slug}/coaches/{role_id}/", response={204: None, **refusals(403, 404)},
               summary="Stop a coach coaching this workspace (owner-only)")
@gate("own", setting=True)
def remove_coach(request: HttpRequest, slug: str, role_id: int):
    """Owner-only. At once: that coach's next read of anyone's sessions through this role is "not found"."""
    m = require_role(request.user, slug, perms.OWN)
    role = next((r for r in coaching.roles_in(m.workspace) if r.pk == role_id), None)
    if role is None:
        raise HttpError(404, "no such coach in this workspace")
    coaching.remove(role, by=request.user)
    return Status(204, None)
