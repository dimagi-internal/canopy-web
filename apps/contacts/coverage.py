"""Is the fleet brain alive? Per-agent coverage of the people brain (canopy#804 v1.1).

WHY. Every earlier attempt at agent memory died QUIETLY: nothing errored, the
writes simply stopped (dimagi-brain: 6 writes in 5 weeks), and nobody noticed
because the absence of a memory looks exactly like a quiet week. So the brain
reports on itself, per agent, over a window, with an explicit verdict — a dead
brain must be loud.

What is counted, for each agent of the workspace, over the last `days`:

* `human_turns` — REAL conversations with the agent (directly or in one of its
  chats): `people.real_conversation_q` — a human talking through chat, email or
  Slack, never a dispatch carrying their name, a huddle, an approval, a schedule
  or a digest. The same rule picks the digest's candidates (canopy#820).
* `human_turns_with_context` — of those, how many were handed a NON-EMPTY
  `person` block: at least one live fact or a non-empty digest, recorded at the
  moment the envelope was built (`PersonAccess.had_context`). Recorded rather
  than re-derived, because "did the agent know anything" is a fact about that
  moment; the facts may have changed since.
* `digest_turns` — the digest turns created in the window (v2: one batch turn
  per agent per day; v1's per-person ones still count), by state: `queued`
  (not finished yet), `done`, `failed` (failed, lost or missed), `cancelled`.
* `facts_written` — facts the agent's login asserted in this workspace.
* `median_digest_age_hours` — for the people who started a human turn with the
  agent in the window: how old their digest in this workspace is now (median;
  null when none of them has one). `people` / `people_with_digest` give the base.

THE RULE (`healthy`), deliberately simple so it can be read off the numbers:

* the digest-turn failure rate — failed / (done + failed) — is under 20 %
  (no finished digest turns = no evidence of failure), AND
* when the agent had at least 10 human turns, it wrote at least one fact.

An agent whose digest is switched off (its own switch, or the global
`PEOPLE_DIGEST_ENABLED`) is reported `enabled: false` and NOT judged: it is
`healthy` with a note saying why, because "switched off" is a decision, not a
fault, and a health check that pages on a decision gets ignored (canopy#820).
The top-level `healthy` is every ENABLED agent's; `digest_enabled_globally`
says whether the fleet-wide switch is on.
"""
from __future__ import annotations

import datetime as dt
import statistics

from django.db.models import Exists, OuterRef
from django.utils import timezone

#: Failure rate at or above which the digest is unhealthy.
MAX_FAILURE_RATE = 0.20
#: Human turns at or above which an agent must have written at least one fact.
MIN_TURNS_FOR_FACTS = 10
#: Window bounds for `days`.
MIN_DAYS, MAX_DAYS = 1, 90


def agent_coverage(agent, *, since: dt.datetime, now: dt.datetime) -> dict:
    from apps.harness.models import Turn
    from apps.harness.people_digest import KEY_PREFIX, enabled_globally

    from . import people
    from .models import PersonAccess, PersonDigest, PersonFact

    ws = agent.workspace_id
    human = (Turn.objects.filter(people.with_agent_q(agent), created_at__gte=since)
             .filter(people.real_conversation_q()))
    had = PersonAccess.objects.filter(turn=OuterRef("pk"), via=PersonAccess.VIA_ENVELOPE,
                                      had_context=True)
    human_ids = list(human.values_list("pk", flat=True))
    human_turns = len(human_ids)
    with_context = (Turn.objects.filter(pk__in=human_ids).filter(Exists(had)).count()
                    if human_ids else 0)

    digests = Turn.objects.filter(agent=agent, created_at__gte=since,
                                  idempotency_key__startswith=f"{KEY_PREFIX}{agent.pk}:")
    by_status: dict[str, int] = {}
    for status in digests.values_list("status", flat=True):
        by_status[status] = by_status.get(status, 0) + 1
    queued = sum(by_status.get(s, 0) for s in Turn.NON_TERMINAL)
    done = by_status.get(Turn.DONE, 0)
    failed = sum(by_status.get(s, 0) for s in (Turn.FAILED, Turn.LOST, Turn.MISSED))
    cancelled = by_status.get(Turn.CANCELLED, 0)

    facts_written = PersonFact.objects.filter(asserted_by_agent=agent, workspace_id=ws,
                                              created_at__gte=since).count()

    # The people behind those turns: an account's person, or a contact's.
    user_ids = set(human.filter(initiator_kind="user").values_list("initiator_user_id", flat=True))
    contact_ids = set(human.filter(initiator_kind="contact")
                      .values_list("initiator_contact_id", flat=True))
    from .models import Contact, Person

    person_ids = set(Person.objects.filter(user_id__in=user_ids).values_list("pk", flat=True))
    person_ids |= set(Contact.objects.filter(pk__in=contact_ids, person__isnull=False)
                      .values_list("person_id", flat=True))
    ages = [(now - updated).total_seconds() / 3600.0
            for updated in PersonDigest.objects.filter(person_id__in=person_ids, workspace_id=ws)
            .values_list("updated_at", flat=True)]
    median_age = round(statistics.median(ages), 1) if ages else None

    enabled = bool(agent.people_digest_enabled) and enabled_globally()
    finished = done + failed
    failure_rate = round(failed / finished, 3) if finished else None
    problems: list[str] = []  # each one makes an ENABLED agent unhealthy
    if enabled and failure_rate is not None and failure_rate >= MAX_FAILURE_RATE:
        problems.append(f"{failed} of {finished} finished digest turns failed "
                        f"(>= {int(MAX_FAILURE_RATE * 100)}%)")
    if enabled and human_turns >= MIN_TURNS_FOR_FACTS and facts_written == 0:
        problems.append(f"{human_turns} real conversations and no facts written")
    notes: list[str] = []  # said, but not judged
    if not enabled:
        notes.append("people digest is switched off for this agent"
                     if not agent.people_digest_enabled else
                     "people digest is switched off fleet-wide (PEOPLE_DIGEST_ENABLED)")
    healthy = not problems
    reasons = problems + notes

    return {
        "agent": agent.slug,
        "enabled": enabled,
        "digest_enabled": enabled,
        "human_turns": human_turns,
        "human_turns_with_context": with_context,
        "context_rate": round(with_context / human_turns, 3) if human_turns else None,
        "digest_turns": {"queued": queued, "done": done, "failed": failed, "cancelled": cancelled},
        "digest_failure_rate": failure_rate,
        "facts_written": facts_written,
        "people": len(person_ids),
        "people_with_digest": len(ages),
        "median_digest_age_hours": median_age,
        "healthy": healthy,
        "reasons": reasons,
    }


def workspace_coverage(workspace_slug: str, *, days: int = 7, agents=None, now=None) -> dict:
    """Coverage for every agent of the workspace (or the given subset)."""
    from apps.agents.models import Agent
    from apps.harness.people_digest import enabled_globally

    days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    now = now or timezone.now()
    since = now - dt.timedelta(days=days)
    if agents is None:
        agents = Agent.objects.filter(workspace_id=workspace_slug)
    rows = [agent_coverage(a, since=since, now=now) for a in agents.order_by("slug")]
    globally = enabled_globally()
    return {
        "workspace": workspace_slug,
        "days": days,
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "digest_enabled_globally": globally,
        "rule": (f"healthy = digest-turn failure rate < {int(MAX_FAILURE_RATE * 100)}% and, "
                 f"with >= {MIN_TURNS_FOR_FACTS} real conversations, >= 1 fact written; "
                 "only ENABLED agents (their own switch and the global one) are judged; "
                 "the workspace is healthy when every enabled agent is"),
        "healthy": all(r["healthy"] for r in rows if r["enabled"]),
        "agents": rows,
    }
