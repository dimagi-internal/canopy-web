"""/api/agents/{slug}/turn-mode-rules — whether an agent's work runs `manual` or `auto`,
per channel, per person, or both.

Mode is a decision about the AGENT, so these routes are gated on the agent and
name no runner. Which box runs the work is routing (`api_routing.py`), gated on
the box. The agent-wide switch is `PATCH /turn-mode` (`api.py`); these rules sit
above it. How a turn's mode is chosen, top rung first: `apps/harness/turn_mode.py`.

Who may write: anyone who may work the agent may make it MORE careful (set a rule
to `manual`, remove an `auto` rule). Anything that may let it act unreviewed (set
a rule to `auto`, remove a `manual` rule) is for the agent's owner and admins
(`route_refusals.refuse_auto_unless_admin`).
"""
from __future__ import annotations

from typing import get_args

from django.db import transaction
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.events import audit
from apps.harness.actors import ACTORLESS_ORIGINS, normalize_actor
from apps.harness.models import TurnModeRule
from apps.harness.schemas import RoutableSource

from .gates import agent_for_read, agent_for_work
from .route_refusals import refuse_auto_unless_admin
from .schemas import TurnModeRuleIn, TurnModeRuleOut

router = Router(auth=session_auth, tags=["agents"])


def _out(rule: TurnModeRule) -> TurnModeRuleOut:
    return TurnModeRuleOut(source=rule.source, actor=rule.actor, turn_mode=rule.turn_mode,
                           set_by_email=rule.set_by.email if rule.set_by_id else "",
                           updated_at=rule.updated_at)


def _key_or_422(source: str, actor: str) -> tuple[str, str]:
    """The (source, actor) a rule is keyed on, normalized; 422 for one that could never match."""
    if source and source not in get_args(RoutableSource):
        raise HttpError(422, f"not a channel a turn can come from: {source!r}")
    if actor:
        normalized = normalize_actor(actor)
        if not normalized:
            raise HttpError(422, f"not an email address: {actor!r}")
        actor = normalized
        if source in ACTORLESS_ORIGINS:
            raise HttpError(422, f"no person is behind {source} turns, so a person's rule can't apply there")
    if not source and not actor:
        raise HttpError(422, "name a channel (source), a person (actor), or both — the mode for "
                             "everything else is the agent's own switch, PATCH /turn-mode")
    return source, actor


def _label(source: str, actor: str) -> str:
    return f"turn-mode rule {source or 'any channel'}/{actor or 'anyone'}"


def _audit(agent, source: str, actor: str, before: str, after: str, request) -> None:
    who = getattr(request.user, "email", "") or "someone"
    scope = f"{agent.slug} {_label(source, actor)}"
    summary = (f"{who} removed {scope}" if not after else f"{who} set {scope} → {after}")
    audit.note_change(scope=scope, before=before, after=after, workspace=agent.workspace, summary=summary)


@router.get("/{slug}/turn-mode-rules", response=list[TurnModeRuleOut],
            summary="List whose work runs manual or auto")
@gate("member")
def list_turn_mode_rules(request: HttpRequest, slug: str) -> list[TurnModeRuleOut]:
    """The agent's turn-mode rules: each says whether work from one channel
    (`source`), one person (`actor`), or one person on one channel runs `manual`
    or `auto`. A turn takes the most specific rule that matches (person and
    channel, then person, then channel), else the agent's own switch. Names no
    runner: which box runs the work is `GET /runner-rules`."""
    agent = agent_for_read(request, slug)
    return [_out(r) for r in agent.turn_mode_rules.select_related("set_by")]


@router.put("/{slug}/turn-mode-rules", response={200: list[TurnModeRuleOut], **refusals(422)},
            summary="Set whether one channel's or one person's work runs manual or auto")
@gate("agent.work", setting=True)
def set_turn_mode_rule(request: HttpRequest, slug: str, payload: TurnModeRuleIn) -> list[TurnModeRuleOut]:
    """Set ONE rule, keyed by (`source`, `actor`), replacing only that rule. Give
    a channel (`source`, e.g. `api` or `email`), a person (`actor`, their email),
    or both. Needs no runner and no rights over one.

    `manual` from anyone who may work the agent; `auto` from its owner or admins
    (unless the rule is already `auto`). A person's `auto` applies only to a
    VERIFIED message from them; otherwise the turn runs manual and says why.
    A source no turn can come from, an actor that is not an email address, a
    person's rule on a channel no person is behind, or a rule naming neither is 422."""
    agent = agent_for_work(request, slug)
    source, actor = _key_or_422(payload.source, payload.actor)
    rule = TurnModeRule.objects.filter(agent=agent, source=source, actor=actor).first()
    before = rule.turn_mode if rule else ""
    if payload.turn_mode == TurnModeRule.AUTO and before != TurnModeRule.AUTO:
        refuse_auto_unless_admin(request, agent, f"setting a {_label(source, actor)} to auto")
    with transaction.atomic():
        TurnModeRule.objects.update_or_create(
            agent=agent, source=source, actor=actor,
            defaults={"turn_mode": payload.turn_mode, "set_by": request.user},
        )
        _audit(agent, source, actor, before, payload.turn_mode, request)
    return list_turn_mode_rules(request, slug)


@router.delete("/{slug}/turn-mode-rules", response={204: None, **refusals(422)},
               summary="Remove one turn-mode rule")
@gate("agent.work", setting=True)
def delete_turn_mode_rule(request: HttpRequest, slug: str, source: str = "", actor: str = ""):
    """Remove the rule for (`source`, `actor`); that work then takes the next rule
    down, or the agent's own switch. Removing an `auto` rule is open to anyone who
    may work the agent; removing a `manual` one may let work run auto, so it is
    for the agent's owner or admins. Idempotent: no such rule is a 204."""
    agent = agent_for_work(request, slug)
    source, actor = _key_or_422(source, actor)
    rule = TurnModeRule.objects.filter(agent=agent, source=source, actor=actor).first()
    if rule is None:
        return Status(204, None)
    if rule.turn_mode == TurnModeRule.MANUAL:
        refuse_auto_unless_admin(request, agent, f"removing a manual {_label(source, actor)}")
    with transaction.atomic():
        rule.delete()
        _audit(agent, source, actor, rule.turn_mode, "", request)
    return Status(204, None)
