"""The people digest — the fleet brain's FORCED write, as one daily batch (v2).

WHY FORCED. Every earlier attempt at agent memory died the same way: the model
had to CHOOSE to remember, and it didn't (dimagi-brain: 6 writes in 5 weeks). So
canopy does not wait to be asked: once a day it starts, for each agent that has
someone new to remember, ONE turn whose prompt is

    /canopy:people-digest --batch --agent <slug> --workspace <slug>

— list the people you have talked to since you last digested them
(`GET /api/people/digest-candidates/?agent=<slug>`), then for each one read those
conversations, record the durable work-context facts and refresh their digest.
canopy-web makes no model calls of its own, so the extraction runs on the fleet.
Design: hal `docs/proposals/2026-10-07-caller-context-brain.md` §3, canopy#804;
v2 is canopy#820.

WHY v2. v1 (2026-10-07) enqueued a turn the moment a human's turn finished. In
one night it (a) opened a full agent session per human turn, most of which
should have written nothing; (b) counted agent DISPATCHES as the human talking,
because a dispatch carries the person it works for as its initiator; and (c)
landed those sessions on the owner's LAPTOP runner. v2 answers each:

* (a) one batched turn per agent per day, and none at all when its candidate
  list is empty — canopy computes that list with one query, no session;
* (b) only REAL conversations count (`apps.contacts.people.real_conversation_q`:
  chat, email, Slack from a human; not api dispatches, huddles, approvals,
  schedules or digests) — the same rule decides who is a candidate and what the
  digest then reads;
* (c) the turn is `routing=cloud_only`: `claim_next_turn` refuses it to every
  runner that is not `Runner.CLOUD`, pin or no pin.

WHEN. canopy has no clock of its own (no celery, no beat): the runners'
heartbeats are it. `maybe_sweep` runs on every heartbeat and does nothing until
`PEOPLE_DIGEST_SWEEP_HOUR_UTC` on a day not yet swept; a cache lock makes that
one sweep per day across processes, and the per-(agent, day) idempotency key
makes a second one harmless anyway.

SWITCHES. `PEOPLE_DIGEST_ENABLED` — fleet-wide, OFF by default since 2026-10-07
(Jonathan flips it) — and an agent's own `Agent.people_digest_enabled`. Both
must be on. The FleetHold does not stop the sweep from ENQUEUING (turns pile up
visibly while held, by design) but no runner claims anything while it is held,
and an agent never has more than one digest turn waiting.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import Turn

logger = logging.getLogger(__name__)

#: The `origin_ref.trigger` (and envelope `trigger.kind`) of a digest turn.
TRIGGER = "people_digest"
#: Every digest turn's idempotency key starts with this, then `<agent pk>:`.
KEY_PREFIX = "people-digest:"
#: The sweep's once-a-day lock (`maybe_sweep`), suffixed with the UTC date.
_LOCK_PREFIX = "people-digest:sweep:"


def enabled_globally() -> bool:
    return bool(getattr(settings, "PEOPLE_DIGEST_ENABLED", False))


def is_digest_turn(turn: Turn) -> bool:
    return (turn.origin_ref or {}).get("trigger") == TRIGGER or (
        turn.idempotency_key or "").startswith(KEY_PREFIX)


def batch_key(agent, day: dt.date) -> str:
    return f"{KEY_PREFIX}{agent.pk}:batch:{day.isoformat()}"


def render_prompt(agent) -> str:
    """The slash command alone on the first line (the runner appends
    `--caller <path>` to it), then a short reminder of what the turn is for."""
    return "\n".join([
        f"/canopy:people-digest --batch --agent {agent.slug} --workspace {agent.workspace_id}",
        "",
        "canopy started this turn on its own (the daily people digest). List the people "
        "you have had real conversations with since you last digested them, then one "
        "person at a time read those conversations, record the durable WORK-CONTEXT "
        "facts (role, project, instance, preference, correction, terminology — nothing "
        "else) and refresh their digest. Do not contact anyone: this turn has no outbound.",
    ])


def maybe_sweep(now: dt.datetime | None = None) -> list[Turn]:
    """The heartbeat's door. A no-op while the global switch is off, before the
    sweep hour, and after today's sweep. Never raises: a digest is never worth a
    runner's heartbeat."""
    if not enabled_globally():
        return []
    now = now or timezone.now()
    utc = now.astimezone(dt.timezone.utc)
    if utc.hour < int(getattr(settings, "PEOPLE_DIGEST_SWEEP_HOUR_UTC", 6)):
        return []
    if not cache.add(f"{_LOCK_PREFIX}{utc.date().isoformat()}", "1", timeout=26 * 3600):
        return []
    try:
        # A savepoint, so a failed sweep cannot poison the heartbeat's transaction.
        with transaction.atomic():
            return sweep(now=now)
    except Exception:  # noqa: BLE001
        logger.exception("people-digest: the daily sweep failed")
        return []


def sweep(now: dt.datetime | None = None) -> list[Turn]:
    """Enqueue today's digest turn for every agent that needs one: both switches
    on, no digest turn of its own still waiting or running, and at least one
    candidate. Returns the turns it enqueued (an already-enqueued one for today
    is not returned again)."""
    from apps.agents.models import Agent
    from apps.contacts import people

    if not enabled_globally():
        return []
    now = now or timezone.now()
    out = []
    agents = (Agent.objects.filter(people_digest_enabled=True, workspace__isnull=False)
              .select_related("workspace", "owner").order_by("slug"))
    for agent in agents:
        pending = Turn.objects.filter(
            agent=agent, idempotency_key__startswith=f"{KEY_PREFIX}{agent.pk}:",
            status__in=Turn.NON_TERMINAL).exists()
        if pending:
            continue
        if not people.digest_candidates(agent, limit=1, now=now):
            continue
        turn, created = enqueue_batch(agent, now=now)
        if created:
            out.append(turn)
    return out


def enqueue_batch(agent, *, now: dt.datetime | None = None) -> tuple[Turn, bool]:
    """Today's digest turn for `agent` (idempotent per UTC day)."""
    from . import initiator as who
    from .services import enqueue_turn

    now = now or timezone.now()
    return enqueue_turn(
        agent=agent,
        origin=Turn.ORIGIN_API,
        idempotency_key=batch_key(agent, now.astimezone(dt.timezone.utc).date()),
        prompt=render_prompt(agent),
        origin_ref={
            "trigger": TRIGGER,
            "batch": True,
            "no_outbound": True,
            "workspace": agent.workspace_id,
        },
        routing=Turn.CLOUD_ONLY,
        initiator=who.system(via="people-digest", accountable=agent.owner),
    )
