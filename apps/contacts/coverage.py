"""Is the fleet brain alive? Per-agent coverage of the people brain (canopy#804 v1.1).

WHY. Every earlier attempt at agent memory died QUIETLY: nothing errored, the
writes simply stopped (dimagi-brain: 6 writes in 5 weeks), and nobody noticed
because the absence of a memory looks exactly like a quiet week. So the brain
reports on itself, per agent, over a window, with an explicit verdict — a dead
brain must be loud.

What is counted, for each agent of the workspace, over the last `days`:

* `human_turns` — turns WITH the agent (directly or in one of its chats) that a
  human started: a canopy user who is not an agent's login or a system account,
  or a contact — and only humans who make recording AVAILABLE
  (`Person.hcp_record_available`; a session may still turn it off, which this
  count does not see). Every other count below is over these turns.
* `human_turns_with_context` — of those, how many were handed a NON-EMPTY
  `person` block (at least one fact), recorded at the moment the envelope was
  built (`PersonAccess.had_context`). Recorded rather than re-derived, because
  "did the agent know anything" is a fact about that moment.
* `facts_written` — facts the agent recorded IN-SESSION: asserted by the agent,
  in this workspace, sourced from one of those human turns. Facts are recorded by
  the session that learned them (HCP `addPreference`, prompted by the person
  block and the turn checklist); there is no separate digest turn since
  2026-10-09.
* `people` — distinct people who started a human turn with the agent.

THE RULE (`healthy`), deliberately simple so it can be read off the numbers:
when the agent had at least 10 human turns, it recorded at least one fact. The
workspace is healthy when every agent is.
"""
from __future__ import annotations

import datetime as dt
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

#: Human turns at or above which an agent must have written at least one fact.
MIN_TURNS_FOR_FACTS = 10
#: Window bounds for `days`.
MIN_DAYS, MAX_DAYS = 1, 90


def human_turn_q(prefix: str = "") -> Q:
    """Turns a HUMAN started: a canopy user who is not an agent's own login nor a
    system account, or a contact. The same rule as `people.initiator_person`,
    as a query."""
    p = prefix
    return (
        Q(**{f"{p}initiator_kind": "contact", f"{p}initiator_contact__isnull": False})
        | Q(**{f"{p}initiator_kind": "user", f"{p}initiator_user__isnull": False,
               f"{p}initiator_user__agent_identity__isnull": True,
               f"{p}initiator_user__system_account__isnull": True})
    )


def memory_on_q(prefix: str = "") -> Q:
    """Turns started by a person who makes recording available (`Person.hcp_record_available`).
    Someone who has it off is not a gap in coverage: nothing may be recorded."""
    p = prefix
    return (Q(**{f"{p}initiator_kind": "user", f"{p}initiator_user__person__hcp_record_available": True})
            | Q(**{f"{p}initiator_kind": "contact",
                   f"{p}initiator_contact__person__hcp_record_available": True}))


def _with_agent(agent) -> Q:
    return Q(agent=agent) | Q(chat_session__agent=agent)


def agent_coverage(agent, *, since: dt.datetime, now: dt.datetime) -> dict:
    from apps.harness.models import Turn

    from .models import Contact, Person, PersonAccess, PersonFact

    ws = agent.workspace_id
    human = (Turn.objects.filter(_with_agent(agent), created_at__gte=since)
             .filter(human_turn_q()).filter(memory_on_q()))
    had = PersonAccess.objects.filter(turn=OuterRef("pk"), via=PersonAccess.VIA_ENVELOPE,
                                      had_context=True)
    human_ids = list(human.values_list("pk", flat=True))
    human_turns = len(human_ids)
    with_context = (Turn.objects.filter(pk__in=human_ids).filter(Exists(had)).count()
                    if human_ids else 0)

    facts_written = (PersonFact.objects.filter(asserted_by_agent=agent, workspace_id=ws,
                                               created_at__gte=since,
                                               source_turn_id__in=human_ids).count()
                     if human_ids else 0)

    # The people behind those turns: an account's person, or a contact's.
    user_ids = set(human.filter(initiator_kind="user").values_list("initiator_user_id", flat=True))
    contact_ids = set(human.filter(initiator_kind="contact")
                      .values_list("initiator_contact_id", flat=True))
    person_ids = set(Person.objects.filter(user_id__in=user_ids).values_list("pk", flat=True))
    person_ids |= set(Contact.objects.filter(pk__in=contact_ids, person__isnull=False)
                      .values_list("person_id", flat=True))

    reasons: list[str] = []
    if human_turns >= MIN_TURNS_FOR_FACTS and facts_written == 0:
        reasons.append(f"{human_turns} human turns and no facts recorded in-session")

    return {
        "agent": agent.slug,
        "human_turns": human_turns,
        "human_turns_with_context": with_context,
        "context_rate": round(with_context / human_turns, 3) if human_turns else None,
        "facts_written": facts_written,
        "people": len(person_ids),
        "healthy": not reasons,
        "reasons": reasons,
    }


def workspace_coverage(workspace_slug: str, *, days: int = 7, agents=None, now=None) -> dict:
    """Coverage for every agent of the workspace (or the given subset)."""
    from apps.agents.models import Agent

    days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    now = now or timezone.now()
    since = now - dt.timedelta(days=days)
    if agents is None:
        agents = Agent.objects.filter(workspace_id=workspace_slug)
    rows = [agent_coverage(a, since=since, now=now) for a in agents.order_by("slug")]
    return {
        "workspace": workspace_slug,
        "days": days,
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "rule": (f"healthy = with >= {MIN_TURNS_FOR_FACTS} human turns, >= 1 fact recorded "
                 "in-session; the workspace is healthy when every agent is. Counts only people "
                 "who let agents learn about them"),
        "healthy": all(r["healthy"] for r in rows),
        "agents": rows,
    }
