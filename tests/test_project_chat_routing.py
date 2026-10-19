"""An unbound project chat runs on a box that has its repo, that the workspace
shares (its runner order) or the chatting person administers.

A project chat is a chat session with no agent, only a repo (`Session.project`).
Its unbound turns used to go to any session-capable box in the tenant, so a
teammate's new project chat could land on someone else's laptop. The rules now
(spec 2026-10-19 #6), applied by the claim and by every reader that mirrors it:

1. the box declares the repo (`Runner.project_names()`);
2. with an enabled workspace runner order, the order's cascade and grace, among
   listed boxes that pass rule 1;
3. with no order, only a box the turn's `enqueued_by` administers — a turn with
   no person behind it gets no box under this rule, so it needs an order.

A binding still claims only on its holder, and a pin still bypasses 1–3.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import claim, routing, services
from apps.harness.models import Runner, RunnerAdmin, Turn, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def team():
    alice, bob = _user("alice"), _user("bob")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=alice)
    WorkspaceMembership.objects.create(workspace=ws, user=alice, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=bob, role=WorkspaceMembership.EDITOR)
    return alice, bob, ws


def _box(owner, name, projects=("canopy-web",), **caps):
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=owner,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "projects": list(projects), **caps},
    ))


def _chat(ws, person, project="canopy-web", *, key="c1", age_seconds=0, enqueued=True):
    session = Session.objects.create(workspace=ws, created_by=person, project=project)
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="hi",
                               enqueued_by=person if enqueued else None)
    if age_seconds:
        Turn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=age_seconds))
        turn.refresh_from_db()
    return turn


def _order(ws, *runners):
    for i, r in enumerate(runners):
        WorkspaceRunnerOrder.objects.create(workspace=ws, runner=r, rank=i, enabled=True)


def _claimed(runner):
    t = services.claim_next_turn(runner)
    return t.pk if t else None


def _refined(runner, turn) -> bool:
    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__runner_binding",
                                        "chat_session__agent", "enqueued_by").get(pk=turn.pk))
    defaults, priorities = claim._assignment_rows_for_turns([turn])
    return claim._refined_allows(runner, turn, defaults, priorities)


def test_box_without_the_repo_does_not_take_it(team):
    alice, _bob, ws = team
    box = _box(alice, "alice-box", projects=("connect-labs",))
    _chat(ws, alice)
    assert _claimed(box) is None


def test_follows_the_workspace_order(team):
    alice, bob, ws = team
    a, b = _box(alice, "a"), _box(bob, "b")
    _order(ws, b, a)
    turn = _chat(ws, alice)
    assert _claimed(a) is None  # rank 1 waits while rank 0 is available
    assert _claimed(b) == turn.pk  # the order shares b even though alice does not run it


def test_the_order_grace_opens_the_next_rank(team):
    alice, bob, ws = team
    a, b = _box(alice, "a"), _box(bob, "b")
    _order(ws, b, a)
    turn = _chat(ws, alice, age_seconds=routing.CASCADE_GRACE_SECONDS + 5)
    assert _claimed(a) == turn.pk


def test_a_box_off_the_order_never_takes_it(team):
    alice, bob, ws = team
    listed, mine = _box(bob, "listed"), _box(alice, "mine")
    _order(ws, listed)
    _chat(ws, alice, age_seconds=3600)
    assert _claimed(mine) is None  # not listed, even though alice runs it


def test_no_order_only_a_box_the_chatter_administers(team):
    alice, bob, ws = team
    bobs = _box(bob, "bob-box")
    alices = _box(alice, "alice-box")
    turn = _chat(ws, alice)
    assert _claimed(bobs) is None
    assert _claimed(alices) == turn.pk


def test_a_runner_admin_counts_as_administering(team):
    alice, bob, ws = team
    bobs = _box(bob, "bob-box")
    RunnerAdmin.objects.create(runner=bobs, user=alice)
    turn = _chat(ws, alice)
    assert _claimed(bobs) == turn.pk


def test_contact_chat_with_no_order_waits_and_says_why(team):
    """Review Focus 3: a turn with no person behind it, in a workspace with no
    runner order, waits — it must not land on an arbitrary box — and the
    unclaimable report says why."""
    alice, _bob, ws = team
    box = _box(alice, "alice-box")
    turn = _chat(ws, alice, enqueued=False, age_seconds=600)
    assert _claimed(box) is None
    rows = {r["turn_id"]: r for r in services.unclaimable_queued_turns(alice)}
    row = rows[str(turn.pk)]
    assert row["kind"] == "config"
    assert "no runner order in canopy" in row["reason"]
    assert claim.turn_reach(turn).kind == claim.UNROUTED
    assert "runner order" in claim.turn_reach(turn).reason


def test_teammates_chat_names_the_person_and_the_repo(team):
    alice, bob, ws = team
    _box(alice, "alice-box")
    turn = _chat(ws, bob, age_seconds=600)
    row = {r["turn_id"]: r for r in services.unclaimable_queued_turns(bob)}[str(turn.pk)]
    assert row["reason"] == ("no runner order in canopy, and bob@dimagi.com administers "
                             "no box with canopy-web")


def test_bound_session_unchanged(team):
    alice, bob, ws = team
    bobs = _box(bob, "bob-box", projects=())
    turn = _chat(ws, alice)
    RunnerBinding.objects.create(session=turn.chat_session, runner=bobs)
    assert _claimed(bobs) == turn.pk


def test_pin_still_wins(team):
    alice, bob, ws = team
    bobs = _box(bob, "bob-box", projects=())
    turn = _chat(ws, alice)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=bobs)
    assert _claimed(bobs) == turn.pk


def test_a_chat_with_no_repo_still_needs_the_order_or_your_box(team):
    """A chat with neither an agent nor a repo has no repo to match (rule 1 is
    vacuous) but is still somebody's work: rules 2–3 hold for it too."""
    alice, bob, ws = team
    bobs, alices = _box(bob, "bob-box", projects=()), _box(alice, "alice-box", projects=())
    turn = _chat(ws, alice, project="")
    assert _claimed(bobs) is None
    assert _claimed(alices) == turn.pk


CASES = ["no_repo", "order_rank0", "order_unlisted", "no_order_other", "no_order_mine",
         "contact", "bound", "pinned", "admin"]


@pytest.mark.parametrize("case", CASES)
def test_reach_and_claim_agree(team, case):
    """`_refined_allows` (what turn_reach / the unclaimable report / topology
    ask) and `claim_next_turn` give the same answer. Turns are past the cascade
    grace, because the cascade is about WHEN, not WHETHER — `_refined_allows`
    asks only the second."""
    alice, bob, ws = team
    old = routing.CASCADE_GRACE_SECONDS + 5
    if case == "no_repo":
        box = _box(alice, "x", projects=("other",))
        turn = _chat(ws, alice, age_seconds=old)
    elif case == "order_rank0":
        box = _box(bob, "x")
        _order(ws, box, _box(alice, "y"))
        turn = _chat(ws, alice, age_seconds=old)
    elif case == "order_unlisted":
        box = _box(alice, "x")
        _order(ws, _box(bob, "y"))
        turn = _chat(ws, alice, age_seconds=old)
    elif case == "no_order_other":
        box = _box(bob, "x")
        turn = _chat(ws, alice, age_seconds=old)
    elif case == "no_order_mine":
        box = _box(alice, "x")
        turn = _chat(ws, alice, age_seconds=old)
    elif case == "contact":
        box = _box(alice, "x")
        turn = _chat(ws, alice, enqueued=False, age_seconds=old)
    elif case == "bound":
        box = _box(bob, "x", projects=())
        turn = _chat(ws, alice, age_seconds=old)
        RunnerBinding.objects.create(session=turn.chat_session, runner=box)
    elif case == "pinned":
        box = _box(bob, "x", projects=())
        turn = _chat(ws, alice, age_seconds=old)
        Turn.objects.filter(pk=turn.pk).update(pinned_runner=box)
    else:  # admin
        box = _box(bob, "x")
        RunnerAdmin.objects.create(runner=box, user=alice)
        turn = _chat(ws, alice, age_seconds=old)
    refined = _refined(box, turn)
    reach_runners = {r.pk for r in claim.turn_reach(turn).runners}
    assert (box.pk in reach_runners) == refined
    assert refined == (_claimed(box) is not None)
