"""A finished conversation starts a digest turn — the brain's FORCED write.

WHY. Every earlier attempt at agent memory died the same way: the model had to
CHOOSE to remember, and it didn't (dimagi-brain: 6 writes in 5 weeks). So canopy
does not wait to be asked. When a turn a human started with an agent finishes,
canopy enqueues a turn for that SAME agent whose prompt is

    /canopy:people-digest --person <id> --workspace <slug> --since <iso>

— read that person's conversations with you since the last digest, record the
durable work-context facts (`POST /api/people/{id}/facts/`) and refresh their
digest (`PUT /api/people/{id}/digest/`). canopy-web makes no model calls of its
own (proposal Q3), so the extraction runs on the runner fleet like any turn.
Design: hal `docs/proposals/2026-10-07-caller-context-brain.md` §3, canopy#804.

The brakes, since a hook that starts turns when turns finish is a feedback loop:

* NEVER for a digest turn itself (`origin_ref.trigger == "people_digest"`), and
  never for a turn canopy or another agent started — a digest turn's initiator
  is `system`, so even a digest turn stripped of its marker cannot retrigger.
* NEVER without an agent (a repo turn, an agentless chat) — there is no agent
  to have remembered anything.
* DEBOUNCED per (agent, person): no new digest turn while one for the pair was
  created in the last `PEOPLE_DIGEST_DEBOUNCE_MINUTES` (default 60). Counted
  from the digest turns themselves (their idempotency-key prefix), like
  auto-debug's caps, so there is no second ledger to drift.
* KILL SWITCH: `PEOPLE_DIGEST_ENABLED=false`. There is no per-agent switch yet:
  `Agent` has no settings document to hang one on, and adding a column for it
  was not worth it before v1 proves itself.

`on_turn_finished` never raises and runs in a savepoint: a digest is never worth
a failed finish.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Turn

logger = logging.getLogger(__name__)

#: The `origin_ref.trigger` (and envelope `trigger.kind`) of a digest turn.
TRIGGER = "people_digest"
#: Every digest turn's idempotency key starts with this, then `<agent>:<person>:`.
KEY_PREFIX = "people-digest:"
#: How far back the FIRST digest for a pair looks.
FIRST_LOOKBACK = dt.timedelta(days=14)


def _key_prefix(agent, person) -> str:
    return f"{KEY_PREFIX}{agent.pk}:{person.pk}:"


def _agent_of(turn: Turn):
    if turn.agent_id:
        return turn.agent
    if turn.chat_session_id and turn.chat_session.agent_id:
        return turn.chat_session.agent
    return None


def is_digest_turn(turn: Turn) -> bool:
    return (turn.origin_ref or {}).get("trigger") == TRIGGER or (
        turn.idempotency_key or "").startswith(KEY_PREFIX)


def render_prompt(person, workspace_slug: str, since: dt.datetime) -> str:
    """The slash command alone on the first line (the runner appends
    `--caller <path>` to it), then a short reminder of what the turn is for."""
    return "\n".join([
        f"/canopy:people-digest --person {person.pk} --workspace {workspace_slug} "
        f"--since {since.isoformat(timespec='seconds')}",
        "",
        "canopy started this turn on its own (people digest) after a conversation "
        "with this person finished. Read your conversations with them since then, "
        "record the durable WORK-CONTEXT facts (role, project, instance, preference, "
        "correction, terminology — nothing else) and refresh their digest. "
        "Do not contact anyone: this turn has no outbound.",
    ])


def on_turn_finished(turn: Turn) -> Turn | None:
    """Called when a turn has just become DONE. Returns the digest turn it
    enqueued, or None. Never raises."""
    try:
        with transaction.atomic():
            return _on_turn_finished(turn)
    except Exception:  # noqa: BLE001
        logger.exception("people-digest: could not handle the finish of turn %s", turn.pk)
        return None


def _on_turn_finished(turn: Turn) -> Turn | None:
    from apps.contacts import people

    from . import initiator as who

    if not getattr(settings, "PEOPLE_DIGEST_ENABLED", True):
        return None
    if turn.status != Turn.DONE or is_digest_turn(turn):
        return None
    if turn.initiator_kind not in (who.USER, who.CONTACT):
        return None
    agent = _agent_of(turn)
    if agent is None or not agent.workspace_id:
        return None
    person = people.initiator_person(turn)
    if person is None:
        return None

    from apps.agents.models import Agent

    # Serialize on the agent's row, so two of its turns finishing at once cannot
    # both read "no digest in the last hour" and enqueue two.
    Agent.objects.select_for_update().filter(pk=agent.pk).first()
    now = timezone.now()
    prefix = _key_prefix(agent, person)
    last = (Turn.objects.filter(idempotency_key__startswith=prefix)
            .order_by("-created_at").first())
    debounce = dt.timedelta(minutes=int(getattr(settings, "PEOPLE_DIGEST_DEBOUNCE_MINUTES", 60)))
    if last is not None and last.created_at > now - debounce:
        return None
    since = last.created_at if last is not None else now - FIRST_LOOKBACK

    from .services import enqueue_turn

    digest, _ = enqueue_turn(
        agent=agent,
        origin=Turn.ORIGIN_API,
        idempotency_key=f"{prefix}{now:%Y%m%dT%H%M%S%f}",
        prompt=render_prompt(person, agent.workspace_id, since),
        origin_ref={
            "trigger": TRIGGER,
            "person_id": person.pk,
            "workspace": agent.workspace_id,
            "since": since.isoformat(),
            "after_turn": str(turn.pk),
            "no_outbound": True,
        },
        initiator=who.system(via="people-digest", accountable=agent.owner),
        parent={"turn": turn},
    )
    return digest
