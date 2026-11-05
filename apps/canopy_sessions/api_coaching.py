"""`POST /api/coaching/facts/`: a coach says what it did, for analytics. Ids only.

The fact is born in the coach's own scripts (`docs/architecture/analytics.md`), so the
narrowness is here: a closed kind, closed words and ids, no text field to put a sentence
in, and only from inside a running, unconfined turn of the agent whose login calls. A
fact about a person also needs that person's live review grant, as far as this turn may
use it (`access.review_grantors`, the same rule as reading their sessions), so a turn
cannot file a fact about someone who never enrolled or someone its asker is not.
"""
from __future__ import annotations

import re
from typing import Literal

from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import ConfigDict, model_validator

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import activity
from apps.harness import turn_asker

from . import access

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


@router.post("/facts/", response=CoachingFactOut, summary="Record a coaching fact")
@gate("agent-login", kind="telemetry")  # the per-turn login of a running, unconfined turn; not a stored token
def record_coaching_fact(request: HttpRequest, payload: CoachingFactIn) -> dict:
    """For an agent's own login inside its running turn: a note went out, how a person
    replied, or a friction pattern was recorded. Recording the same fact again changes
    nothing (`recorded` is false)."""
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
        if not access.review_grantors(user).filter(agent=agent, user_id=payload.person_id).exists():
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
