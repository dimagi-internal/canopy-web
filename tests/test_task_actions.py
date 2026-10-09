"""Task services: batch create, the five actions, filters, and the agent's drain queue."""
import pytest
from django.contrib.auth.models import User

from apps.agents import services
from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.harness.dispatch_marker import DISPATCH_MARKER, HUMAN_REPLY_OPEN
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    return u, ws, agent


def _review(agent, **over):
    p = {"title": "Send the EOI?", "ask_kind": "review", "idempotency_key": "k1"}
    p.update(over)
    return services.create_tasks(agent, [p])[0]


def _act(task, u, ws, action, comment="", *, editor=True):
    # Workspace's primary key is its slug, which is what dispatch compares against.
    return services.act(task, action=action, comment=comment, by=u.email, by_user=u,
                        actor_workspace_ids={ws.pk}, may_start_turns=editor)


def test_batch_create_replays_by_key_and_creates_new(world):
    _u, _ws, agent = world
    a = services.create_tasks(agent, [{"title": "A", "idempotency_key": "k1"}])
    b = services.create_tasks(agent, [{"title": "A", "idempotency_key": "k1"},
                                      {"title": "B", "idempotency_key": "k2"}])
    assert b[0].pk == a[0].pk and b[1].ext_id == "T2"
    assert agent.tasks.count() == 2


def test_approve_without_on_approve_starts_the_card_turn(world):
    """Approve ALWAYS starts the work: no `on_approve` → one turn written from the
    card, through the same enqueue, and the row is applied (the turn IS the follow-up)."""
    u, ws, agent = world
    task = _review(agent, next_action="Draft the EOI", plan="1. read the call\n2. draft")
    task, row, turns = _act(task, u, ws, "approve", "keep it to one page")
    assert task.status == AgentTask.IN_PROGRESS and not task.ask_is_open
    assert row.status == AgentTaskAction.APPLIED and row.applied_at is not None
    assert len(turns) == 1 and Turn.objects.count() == 1
    turn = turns[0]
    assert turn.agent == agent and turn.raised_from_task_id == task.pk
    assert turn.idempotency_key == f"task-{task.pk}-approve-{row.pk}"
    assert turn.origin_ref["trigger"] == "task_approve" and turn.origin_ref["task"] == task.ext_id
    assert turn.prompt.startswith(f"Work task {task.ext_id}: Send the EOI?")
    assert "Next action: Draft the EOI" in turn.prompt and "2. draft" in turn.prompt
    # Stamped as the agent's card, with the approver's words delimited after it.
    assert DISPATCH_MARKER in turn.prompt and HUMAN_REPLY_OPEN in turn.prompt
    assert "APPROVED BY jj@dimagi.com" in turn.prompt and "keep it to one page" in turn.prompt
    assert task.dispatched_at is not None


def test_approve_by_a_viewer_still_starts_the_work(world):
    """The viewer tier exists to decide; an approval that started nothing for a
    viewer would be the bug this fixes, kept for half the users."""
    u, ws, agent = world
    _task, row, turns = _act(_review(agent), u, ws, "approve", editor=False)
    assert len(turns) == 1 and row.status == AgentTaskAction.APPLIED


def test_nudge_after_approve_reuses_the_turn_that_has_not_started(world):
    """No double-enqueue: a not-yet-started turn of the same task is reused — the
    guard for a double-click, two tabs, or approve-then-nudge."""
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "work"}])[0]
    _t, _row, first = _act(task, u, ws, "approve")
    _t, row, second = _act(task, u, ws, "nudge")
    _t, _row, third = _act(task, u, ws, "nudge")
    assert first == second == third and Turn.objects.count() == 1
    assert row.status == AgentTaskAction.APPLIED


def test_nudge_once_the_turn_is_running_queues_another(world):
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "work", "status": AgentTask.IN_PROGRESS}])[0]
    _t, _row, (first,) = _act(task, u, ws, "nudge")
    Turn.objects.filter(pk=first.pk).update(status=Turn.RUNNING)
    _t, _row, (second,) = _act(task, u, ws, "nudge")
    assert second.pk != first.pk and Turn.objects.count() == 2


def test_approve_with_on_approve_dispatches_and_is_applied(world):
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "/eva:turn go"}])
    task, row, turns = _act(task, u, ws, "approve", "go")
    assert len(turns) == 1 and row.status == AgentTaskAction.APPLIED
    assert task.dispatched_at is not None


def test_bad_on_approve_rolls_back_and_ask_stays_open(world):
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "x", "target_agent": "nobody"}])
    with pytest.raises(ValueError):
        _act(task, u, ws, "approve")
    task.refresh_from_db()
    assert task.ask_is_open and task.status == AgentTask.SUGGESTED
    assert not task.actions.exists() and not Turn.objects.exists()


def test_bad_second_on_approve_rolls_back_the_first_turn(world):
    """The first spec IS enqueued before the second one raises — the rollback has
    to take that turn with it, or the work runs once and the ask stays open."""
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "/eva:turn go"},
                                      {"prompt": "x", "target_agent": "nobody"}])
    with pytest.raises(ValueError):
        _act(task, u, ws, "approve")
    task.refresh_from_db()
    assert task.ask_is_open and task.status == AgentTask.SUGGESTED
    assert Turn.objects.count() == 0 and not task.actions.exists()


@pytest.mark.parametrize("finished", [AgentTask.DONE, AgentTask.DECLINED])
@pytest.mark.parametrize("action", ["approve", "decline"])
def test_approve_or_decline_on_a_finished_plain_task_is_refused(world, finished, action):
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "shipped", "status": finished}])[0]
    with pytest.raises(services.ClosedAskError):
        _act(task, u, ws, action)
    task.refresh_from_db()
    assert task.status == finished and not task.actions.exists()


def test_approve_on_a_suggested_plain_task_starts_it(world):
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "work"}])[0]
    task, row, turns = _act(task, u, ws, "approve")
    assert task.status == AgentTask.IN_PROGRESS and row.action == AgentTaskAction.APPROVE
    assert len(turns) == 1


def test_approve_on_an_in_progress_plain_task_is_refused(world):
    """Approving starts a turn, so a second approval would be a viewer's nudge."""
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "work", "status": AgentTask.IN_PROGRESS}])[0]
    with pytest.raises(services.ClosedAskError, match="nudge"):
        _act(task, u, ws, "approve")
    assert not task.actions.exists() and not Turn.objects.exists()


def test_second_approve_on_closed_ask_is_refused(world):
    u, ws, agent = world
    task = _review(agent)
    _act(task, u, ws, "decline", "dup")
    with pytest.raises(services.ClosedAskError):
        _act(task, u, ws, "approve")


def test_stale_copy_cannot_act_twice(world):
    """Two tabs: both loaded the task while its ask was open. The second click must
    see the row as it is NOW, not the copy it was handed."""
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "/eva:turn go"}])
    tab_a = AgentTask.objects.get(pk=task.pk)
    tab_b = AgentTask.objects.get(pk=task.pk)
    _act(tab_a, u, ws, "approve")
    with pytest.raises(services.ClosedAskError):
        _act(tab_b, u, ws, "approve")
    assert Turn.objects.count() == 1 and task.actions.count() == 1


def test_decline_closes_and_keeps_reason(world):
    u, ws, agent = world
    task, row, _ = _act(_review(agent), u, ws, "decline", "duplicate of T4")
    assert task.status == AgentTask.DECLINED and row.comment == "duplicate of T4"
    assert row.status == AgentTaskAction.APPLIED


def test_reply_answers_a_question_but_not_a_review(world):
    u, ws, agent = world
    q = _review(agent, ask_kind="question", idempotency_key="q")
    q, row, turns = _act(q, u, ws, "reply", "Tuesday")
    # The Answer behaviour is unchanged: no `on_approve` → a pending answer, no turn.
    assert not q.ask_is_open and row.status == AgentTaskAction.PENDING and turns == []
    r = _review(agent, idempotency_key="r")
    r, _row, _ = _act(r, u, ws, "reply", "what's the budget?")
    assert r.ask_is_open


def test_answer_with_on_approve_runs_it_with_the_answer(world):
    u, ws, agent = world
    q = _review(agent, ask_kind="question", on_approve=[{"prompt": "/eva:turn book it"}])
    q, row, turns = _act(q, u, ws, "reply", "Tuesday")
    assert len(turns) == 1 and row.status == AgentTaskAction.APPLIED
    assert "ANSWERED BY" in turns[0].prompt and "Tuesday" in turns[0].prompt
    assert q.status == AgentTask.IN_PROGRESS


def test_editor_reply_on_a_live_task_starts_a_turn_carrying_it(world):
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "Brand voice", "next_action": "Draft"}])[0]
    task, row, turns = _act(task, u, ws, "reply", "start with the last three edits")
    assert len(turns) == 1 and row.status == AgentTaskAction.APPLIED
    assert task.status == AgentTask.SUGGESTED  # a note changes no status
    turn = turns[0]
    assert turn.origin_ref["trigger"] == "task_reply"
    assert turn.idempotency_key == f"task-{task.pk}-reply-{row.pk}"
    assert "left a note on task" in turn.prompt
    assert "NOTE FROM jj@dimagi.com" in turn.prompt
    assert "start with the last three edits" in turn.prompt


def test_two_replies_are_two_turns(world):
    """A reply's words are new — it never folds into a queued turn that lacks them."""
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "x", "status": AgentTask.IN_PROGRESS}])[0]
    _act(task, u, ws, "reply", "one")
    _act(task, u, ws, "reply", "two")
    assert Turn.objects.count() == 2


def test_viewer_reply_stays_a_note(world):
    """Only the tier that may start a turn wakes the agent with a reply; a
    viewer's note waits in the agent's queue for its next turn."""
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "x", "status": AgentTask.IN_PROGRESS}])[0]
    task, row, turns = _act(task, u, ws, "reply", "fyi", editor=False)
    assert turns == [] and row.status == AgentTaskAction.PENDING
    assert not Turn.objects.exists()


def test_empty_reply_is_refused(world):
    u, ws, agent = world
    with pytest.raises(ValueError):
        _act(_review(agent), u, ws, "reply", "  ")


def test_unknown_action_is_refused(world):
    u, ws, agent = world
    with pytest.raises(ValueError):
        _act(_review(agent), u, ws, "defer")


def test_nudge_starts_a_turn_without_changing_status(world):
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "Ideas backlog", "status": AgentTask.IN_PROGRESS,
                                          "next_action": "Append new ideas"}])[0]
    task, row, turns = _act(task, u, ws, "nudge")
    assert task.status == AgentTask.IN_PROGRESS and row.status == AgentTaskAction.APPLIED
    assert len(turns) == 1 and turns[0].origin_ref["trigger"] == "task_nudge"
    assert "nudged this in-progress task" in turns[0].prompt
    assert "Next action: Append new ideas" in turns[0].prompt


@pytest.mark.parametrize("status", [AgentTask.SUGGESTED, AgentTask.DONE, AgentTask.DECLINED])
def test_nudge_off_an_in_progress_task_is_refused(world, status):
    """A suggested task is started by approving it; nudging would start unapproved work."""
    u, ws, agent = world
    task = services.create_tasks(agent, [{"title": "x", "status": status}])[0]
    with pytest.raises(services.ClosedAskError):
        _act(task, u, ws, "nudge")
    assert not task.actions.exists() and not Turn.objects.exists()


def test_dispatch_is_gone(world):
    u, ws, agent = world
    with pytest.raises(ValueError):
        _act(_review(agent), u, ws, "dispatch")


def test_done_closes(world):
    u, ws, agent = world
    t, row, turns = _act(_review(agent), u, ws, "done")
    assert t.status == AgentTask.DONE and not t.ask_is_open
    assert turns == [] and row.status == AgentTaskAction.APPLIED


def test_decline_starts_nothing(world):
    u, ws, agent = world
    _t, _row, turns = _act(_review(agent), u, ws, "decline", "no")
    assert turns == [] and not Turn.objects.exists()


def test_drain_queue(world):
    u, ws, agent = world
    # A viewer's note is what still lands in the agent's queue.
    task = services.create_tasks(agent, [{"title": "x", "status": AgentTask.IN_PROGRESS}])[0]
    _t, row, _ = _act(task, u, ws, "reply", "fyi", editor=False)
    assert list(services.pending_actions(agent)) == [row]
    services.mark_applied(row, "sent")
    assert not services.pending_actions(agent).exists()
    row.refresh_from_db()
    assert row.result_note == "sent" and row.applied_at is not None


def test_get_task_by_ref_is_case_insensitive(world):
    _u, _ws, agent = world
    t = services.create_tasks(agent, [{"title": "A"}])[0]
    assert services.get_task(agent, t.ext_id.lower()).pk == t.pk
    assert services.get_task(agent, "T99") is None


def test_waiting_me_includes_parked_and_unrouted_asks(world):
    u, ws, agent = world
    other = User.objects.create_user("an", "andrea@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.VIEWER)
    parked = services.create_tasks(agent, [{"title": "parked on me", "waiting_on_email": u.email,
                                            "status": "in_progress"}])[0]
    unrouted = _review(agent, idempotency_key="unrouted")
    services.create_tasks(agent, [{"title": "parked on Andrea", "waiting_on_email": other.email,
                                   "status": "in_progress"}])
    services.create_tasks(agent, [{"title": "just work", "status": "in_progress"}])

    got = set(services.filter_tasks(agent.tasks.all(), user=u, waiting="me"))
    assert got == {parked, unrouted}


def test_filter_by_project_status_ask_and_batch(world):
    _u, _ws, agent = world
    p = services.create_project(agent, type("D", (), {"name": "IDM talk"})())
    in_p = services.create_tasks(agent, [{"title": "a", "project": p.ext_id, "batch_key": "b1"}])[0]
    loose = _review(agent, idempotency_key="loose")
    qs = agent.tasks.all()
    assert list(services.filter_tasks(qs, project=p.ext_id.lower())) == [in_p]
    assert list(services.filter_tasks(qs, project="none")) == [loose]
    assert list(services.filter_tasks(qs, ask="open")) == [loose]
    assert list(services.filter_tasks(qs, batch="b1")) == [in_p]
    assert set(services.filter_tasks(qs, status="suggested,done")) == {in_p, loose}


def test_unknown_project_files_nowhere(world):
    _u, _ws, agent = world
    t = services.create_tasks(agent, [{"title": "a", "project": "P77"}])[0]
    assert t.project is None


def test_unknown_waiting_on_rolls_back_the_batch(world):
    _u, _ws, agent = world
    with pytest.raises(services.UnknownPersonError):
        services.create_tasks(agent, [{"title": "a"},
                                      {"title": "b", "waiting_on_email": "stranger@x.com"}])
    assert not agent.tasks.exists()
