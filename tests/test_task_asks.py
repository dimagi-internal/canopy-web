"""A task can ask a human for something — what an `Item` used to be.

The three verbs and their guarantees are Item's, because the failures they were
built from are real: answering a question used to be inert (three answered
cards, zero turns, 2026-07-30), deciding twice would dispatch twice, and a
decision committed before its dispatch left work permanently unqueued.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import services
from apps.agents.models import Agent, AgentTask
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    human = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=human)
    WorkspaceMembership.objects.create(user=human, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=human)
    return human, ws, agent


def _ask(agent, **over):
    payload = {"title": "Send the EOI?", "ask_kind": AgentTask.ASK_REVIEW,
               "ask_body": "Draft attached.", "idempotency_key": over.pop("key", "k1")}
    payload.update(over)
    return services.raise_asks(agent=agent, payloads=[payload])[0]


def _slugs(ws):
    return {ws.slug}


# --- raising ---------------------------------------------------------------


def test_an_ask_lands_on_the_board_as_a_suggested_task(world):
    _human, _ws, agent = world

    task = _ask(agent)

    assert task.status == AgentTask.SUGGESTED
    assert task.ask_is_open
    assert task.ext_id == "T1"


def test_raising_the_same_key_twice_replays(world):
    """A producer re-posting its batch (a retried audit) gets the same rows."""
    _human, _ws, agent = world

    first = _ask(agent)
    second = _ask(agent)

    assert first.pk == second.pk
    assert AgentTask.objects.count() == 1


def test_a_task_with_no_ask_is_not_asking_anything(world):
    _human, _ws, agent = world
    task = AgentTask.objects.create(agent=agent, ext_id="T9", title="just work")

    assert not task.ask_is_open


# --- deciding a review -----------------------------------------------------


def test_implement_dispatches_the_work_and_hands_the_agent_the_ball(world):
    human, ws, agent = world
    task = _ask(agent, dispatch=[{"prompt": "/eva:turn send it"}])

    task, turns = services.decide_ask(
        task, decision=AgentTask.IMPLEMENT, comment="go", by="jj",
        actor_workspace_slugs=_slugs(ws), decided_by_user=human,
    )

    assert len(turns) == 1
    assert turns[0].raised_from_task_id == task.pk
    assert task.status == AgentTask.IN_PROGRESS
    assert not task.ask_is_open
    assert task.dispatched_at is not None


def test_skip_declines_the_task_and_dispatches_nothing(world):
    human, ws, agent = world
    task = _ask(agent, dispatch=[{"prompt": "/eva:turn send it"}])

    task, turns = services.decide_ask(
        task, decision=AgentTask.SKIP, comment="", by="jj",
        actor_workspace_slugs=_slugs(ws), decided_by_user=human,
    )

    assert turns == [] and Turn.objects.count() == 0
    assert task.status == AgentTask.DECLINED


def test_defer_stops_the_asking_but_keeps_the_card(world):
    """Not now is not never: the ask closes, the work stays on the board."""
    human, ws, agent = world
    task = _ask(agent)

    task, _turns = services.decide_ask(
        task, decision=AgentTask.DEFER, comment="after the summit", by="jj",
        actor_workspace_slugs=_slugs(ws), decided_by_user=human,
    )

    assert task.status == AgentTask.SUGGESTED
    assert not task.ask_is_open


def test_a_bad_verb_is_refused(world):
    human, ws, agent = world
    task = _ask(agent)

    with pytest.raises(ValueError):
        services.decide_ask(task, decision="maybe", comment="", by="jj",
                            actor_workspace_slugs=_slugs(ws), decided_by_user=human)


def test_deciding_twice_is_refused_so_work_cannot_dispatch_twice(world):
    human, ws, agent = world
    task = _ask(agent, dispatch=[{"prompt": "/eva:turn send it"}])
    services.decide_ask(task, decision=AgentTask.IMPLEMENT, comment="", by="jj",
                        actor_workspace_slugs=_slugs(ws), decided_by_user=human)

    with pytest.raises(services.AlreadyDecidedError):
        services.decide_ask(task, decision=AgentTask.IMPLEMENT, comment="", by="jj",
                            actor_workspace_slugs=_slugs(ws), decided_by_user=human)

    assert Turn.objects.count() == 1


# --- deciding a question ---------------------------------------------------


def test_an_answer_is_the_go_ahead_and_dispatches(world):
    """A question has no verb to click, so the answer IS the approval. This was
    inert once: answered cards produced no turns at all."""
    human, ws, agent = world
    task = _ask(agent, ask_kind=AgentTask.ASK_QUESTION, title="Which venue?",
                dispatch=[{"prompt": "/eva:turn book it"}])

    task, turns = services.decide_ask(
        task, decision="", comment="The Hilton", by="jj",
        actor_workspace_slugs=_slugs(ws), decided_by_user=human,
    )

    assert len(turns) == 1
    assert task.decision == ""          # a question never carries a verb
    assert not task.ask_is_open         # …and is closed by `decided_at`
    assert "The Hilton" in turns[0].prompt


def test_an_empty_answer_is_refused(world):
    human, ws, agent = world
    task = _ask(agent, ask_kind=AgentTask.ASK_QUESTION)

    with pytest.raises(ValueError):
        services.decide_ask(task, decision="", comment="   ", by="jj",
                            actor_workspace_slugs=_slugs(ws), decided_by_user=human)


def test_a_question_with_nothing_to_dispatch_just_records_the_answer(world):
    human, ws, agent = world
    task = _ask(agent, ask_kind=AgentTask.ASK_QUESTION)

    task, turns = services.decide_ask(
        task, decision="", comment="yes", by="jj",
        actor_workspace_slugs=_slugs(ws), decided_by_user=human,
    )

    assert turns == []
    assert task.comment == "yes" and not task.ask_is_open


# --- a bad dispatch spec ---------------------------------------------------


def test_a_bad_spec_leaves_the_ask_open_and_retryable(world):
    """Atomic on purpose: committing the decision first would close the ask with
    its work never queued, and deciding twice is refused — so it could never be
    fixed."""
    human, ws, agent = world
    task = _ask(agent, dispatch=[{"prompt": "x", "target_agent": "nobody"}])

    with pytest.raises(Exception):
        services.decide_ask(task, decision=AgentTask.IMPLEMENT, comment="", by="jj",
                            actor_workspace_slugs=_slugs(ws), decided_by_user=human)

    task.refresh_from_db()
    assert task.ask_is_open
    assert Turn.objects.count() == 0


# --- dismissing ------------------------------------------------------------


def test_dismissing_retires_an_ask_with_its_reason(world):
    human, _ws, agent = world
    task = _ask(agent)

    task = services.dismiss_ask(task, by="jj", decided_by_user=human, comment="already shipped")

    assert task.status == AgentTask.DECLINED
    assert task.comment == "already shipped"
    assert not task.ask_is_open


def test_a_decided_ask_cannot_be_dismissed(world):
    """It would overwrite who approved it while its turns keep running."""
    human, ws, agent = world
    task = _ask(agent, dispatch=[{"prompt": "/eva:turn go"}])
    services.decide_ask(task, decision=AgentTask.IMPLEMENT, comment="", by="jj",
                        actor_workspace_slugs=_slugs(ws), decided_by_user=human)

    with pytest.raises(services.AlreadyDecidedError):
        services.dismiss_ask(task, by="someone else")


# --- the inbox -------------------------------------------------------------


def test_the_inbox_is_tasks_waiting_on_this_person(world):
    human, _ws, agent = world
    other = User.objects.create_user("b", "b@dimagi.com", "pw")
    mine = _ask(agent, key="k-mine", waiting_on_user=human)
    _ask(agent, key="k-theirs", waiting_on_user=other)
    _ask(agent, key="k-nobody")

    waiting = list(services.tasks_waiting_on(human))

    assert [t.pk for t in waiting] == [mine.pk]


def test_answering_takes_it_out_of_the_inbox(world):
    human, ws, agent = world
    task = _ask(agent, waiting_on_user=human)

    services.decide_ask(task, decision=AgentTask.SKIP, comment="", by="jj",
                        actor_workspace_slugs=_slugs(ws), decided_by_user=human)

    assert list(services.tasks_waiting_on(human)) == []


def test_free_text_assigned_is_kept_but_is_not_the_inbox(world):
    """The fleet's boards spell one human three ways and sometimes write a
    sentence ("operator (restart, then the build runs)"). That text survives;
    the inbox routes on the person."""
    human, _ws, agent = world
    task = _ask(agent, assigned="Jonathan Jackson")

    assert task.assigned == "Jonathan Jackson"
    assert list(services.tasks_waiting_on(human)) == []


# --- waiting on a real person ---------------------------------------------


def test_a_task_can_wait_on_a_person_without_asking_anything(world):
    """Most of what a board holds: "waiting on Andrea for the numbers" is a
    wait, and it reached no inbox at all until a task could name a person."""
    human, _ws, agent = world
    task = AgentTask.objects.create(agent=agent, ext_id="T5", title="EOI numbers",
                                    assigned="Andrea", waiting_on_user=human)

    assert not task.ask_is_open                    # nothing is being ASKED
    assert list(services.tasks_waiting_on(human)) == [task]


def test_a_finished_task_waits_on_nobody(world):
    human, _ws, agent = world
    AgentTask.objects.create(agent=agent, ext_id="T6", title="done thing",
                             status=AgentTask.DONE, waiting_on_user=human)

    assert list(services.tasks_waiting_on(human)) == []


def test_routing_a_wait_to_a_stranger_is_refused(world):
    """A wait that looks routed but reaches nobody is the failure the field
    exists to end, so an unknown person is refused rather than dropped."""
    _human, _ws, agent = world
    task = AgentTask.objects.create(agent=agent, ext_id="T7", title="x")

    with pytest.raises(services.UnknownPersonError):
        services.patch_task(task, {"waiting_on_email": "stranger@example.org"})


def test_routing_a_wait_to_a_member_sticks(world):
    human, _ws, agent = world
    task = AgentTask.objects.create(agent=agent, ext_id="T8", title="x")

    services.patch_task(task, {"waiting_on_email": human.email.upper()})

    task.refresh_from_db()
    assert task.waiting_on_user == human
    assert list(services.tasks_waiting_on(human)) == [task]


def test_clearing_the_wait_takes_it_out_of_the_inbox(world):
    human, _ws, agent = world
    task = AgentTask.objects.create(agent=agent, ext_id="T9", title="x", waiting_on_user=human)

    services.patch_task(task, {"waiting_on_email": ""})

    assert list(services.tasks_waiting_on(human)) == []


def test_the_badge_counts_asks_and_parked_tasks_through_one_predicate(world):
    """The inbox, the waiting badge and push all read `waiting_q`. Three copies
    of this question is exactly what this codebase has paid for before."""
    human, _ws, agent = world
    _ask(agent, key="k-ask")                                     # an open ask
    AgentTask.objects.create(agent=agent, ext_id="T10", title="parked",
                             waiting_on_user=human)              # parked on a person
    AgentTask.objects.create(agent=agent, ext_id="T11", title="agent's own work",
                             status=AgentTask.IN_PROGRESS)       # neither

    assert AgentTask.objects.filter(services.waiting_q(), agent=agent).count() == 2
