"""A directed-only box takes only what is put on it, and choosing a box for a
project chat needs you to administer it (spec 2026-10-19 #6).

`capabilities.directed` is box-declared (`RUNNER_DIRECTED=1`) and can only make a
box take less: it claims turns pinned to it and turns of sessions bound to it —
no routed agent turn (even with an enabled assignment row), no repo turn, no
unbound chat, no inherited workspace order. A list that names it must say so
rather than count it as serving (topology), and it must not stall a lower rank as
a "better" one that is available.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import routing, topology
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, Turn, WorkspaceRunnerOrder
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


def _box(owner, name, *, directed=False, projects=("canopy-web",)):
    caps = {"sessions": True, "projects": list(projects)}
    if directed:
        caps["directed"] = True
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=owner,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities=caps,
    ))


def _chat(ws, person, key, project="canopy-web"):
    session = Session.objects.create(workspace=ws, created_by=person, project=project)
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="hi", enqueued_by=person)


def _claimed(runner):
    t = claim.claim_next_turn(runner)
    return t.pk if t else None


def test_directed_box_takes_only_pins_and_bindings(team):
    """Review Focus 4: an enabled assignment row, a workspace order naming it, a
    repo turn for its project and an unbound session of its owner — none of them.
    A pin and a binding — both."""
    alice, _bob, ws = team
    box = _box(alice, "cloud-ec2-test", directed=True)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=alice)
    RunnerAssignment.objects.create(agent=agent, runner=box, rank=0, enabled=True)
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=box, rank=0, enabled=True)
    Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="a", prompt="go")
    Turn.objects.create(project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API,
                        idempotency_key="r", prompt="go")
    _chat(ws, alice, "s")
    assert _claimed(box) is None

    pinned = Turn.objects.create(project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API,
                                 idempotency_key="p", prompt="go", pinned_runner=box)
    assert _claimed(box) == pinned.pk

    bound = _chat(ws, alice, "b")
    RunnerBinding.objects.create(session=bound.chat_session, runner=box)
    assert _claimed(box) == bound.pk


def test_a_directed_box_is_no_better_rank(team):
    """Listed first, it neither claims nor holds back the box after it."""
    alice, _bob, ws = team
    directed, next_box = _box(alice, "test-box", directed=True), _box(alice, "cloud-ec2-2")
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=directed, rank=0, enabled=True)
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=next_box, rank=1, enabled=True)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=alice)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="a",
                               prompt="go")
    repo = Turn.objects.create(project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API,
                               idempotency_key="r", prompt="go")
    assert {_claimed(next_box), _claimed(next_box)} == {turn.pk, repo.pk}


def test_a_directed_box_routes_no_agent(team):
    alice, _bob, ws = team
    box = _box(alice, "test-box", directed=True)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=alice)
    RunnerAssignment.objects.create(agent=agent, runner=box, rank=0, enabled=True)
    assert routing.runner_routes_agent(box, agent) is False
    assert list(routing.agents_routed_to(box)) == []


def test_topology_marks_a_directed_box(team):
    alice, _bob, ws = team
    box = _box(alice, "test-box", directed=True)
    other = _box(alice, "cloud-ec2-2")
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=box, rank=0, enabled=True)
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=other, rank=1, enabled=True)
    Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=alice)
    body = topology.build(ws)
    rows = {r["name"]: r for r in body["runners"]}
    assert rows["test-box"]["directed"] is True
    assert rows["cloud-ec2-2"]["directed"] is False
    agent = body["workspaces"][0]["agents"][0]
    # Never drawn as serving the agent it follows the order of, and named.
    assert [r["runner_id"] for r in agent["routes"]] == [other.pk]
    assert agent["follows"]["directed"] == [box.pk]
    assert rows["test-box"]["agent_count"] == 0


def test_project_chat_runner_id_needs_runner_admin(team):
    alice, bob, _ws = team
    alices = _box(alice, "alice-box")
    c = Client()
    c.force_login(bob)
    r = c.post("/api/canopy-sessions/", data={"project": "canopy-web", "runner_id": str(alices.id)},
               content_type="application/json")
    assert r.status_code == 403, r.content
    c.force_login(alice)
    r = c.post("/api/canopy-sessions/", data={"project": "canopy-web", "runner_id": str(alices.id)},
               content_type="application/json")
    assert r.status_code == 200, r.content


def test_explicit_placement_of_project_chat_needs_runner_admin(team):
    alice, bob, ws = team
    alices = _box(alice, "alice-box")
    session = Session.objects.create(workspace=ws, created_by=bob, project="canopy-web")
    with pytest.raises(ValueError, match="administers"):
        chat.send_message(session=session, text="hi", user=bob, client_id="c1",
                          placement=str(alices.id))
    _msg, turn = chat.send_message(session=session, text="hi", user=bob, client_id="c2")
    with pytest.raises(ValueError, match="administers"):
        chat.place_queued_turn(session=session, placement=str(alices.id), user=bob)
    chat.place_queued_turn(session=session, placement=str(alices.id), user=alice)
    turn.refresh_from_db()
    assert turn.pinned_runner_id == alices.id


def test_offered_cloud_box_is_one_the_sender_administers(team):
    """The "move it to a cloud box" offer for a project chat names only a box
    its sender could place it on."""
    alice, bob, ws = team
    _box(alice, "alice-cloud")
    session = Session.objects.create(workspace=ws, created_by=bob, project="canopy-web")
    assert chat.available_cloud_runner(session, user=bob) is None
    assert chat.available_cloud_runner(session, user=alice).name == "alice-cloud"


def test_the_cloud_offer_never_names_a_directed_box(team):
    """A test box takes work only when someone chooses it: the automatic "move it
    to a cloud box" offer skips it (the explicit pickers still list it)."""
    alice, _bob, ws = team
    _box(alice, "a-test-box", directed=True)
    session = Session.objects.create(workspace=ws, created_by=alice, project="canopy-web")
    assert chat.available_cloud_runner(session, user=alice) is None
    _box(alice, "cloud-ec2-2")
    assert chat.available_cloud_runner(session, user=alice).name == "cloud-ec2-2"


def test_slack_move_to_cloud_offers_a_project_chat_its_senders_box(team):
    """`_move_to_cloud` asks for the offer AS the person moving it; without the
    user every project chat read as "no cloud runner"."""
    from apps.slack import services as slack

    alice, _bob, ws = team
    cloud = _box(alice, "cloud-ec2-2")
    session = Session.objects.create(workspace=ws, created_by=alice, project="canopy-web")
    turn = _chat(ws, alice, "q")
    Turn.objects.filter(pk=turn.pk).update(chat_session=session)
    out = slack._move_to_cloud(session, alice, "slack")
    assert out.status == slack.MOVED, out
    turn.refresh_from_db()
    assert turn.pinned_runner_id == cloud.pk
