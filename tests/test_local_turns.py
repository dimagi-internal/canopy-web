"""Direct use of an agent: a private runner, and the turn a session there opens.

canopy-web#1704. A person runs an agent in their own Claude Code; canopy records
it as a real turn (`origin=local`) on their private runner, with the same envelope
and turn mode a runner turn would get — and it must never hold up the agent's
runner queue.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.harness import routing
from apps.harness.models import Runner, Turn, PrivateRunner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def world():
    jj = _user("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    editor, viewer, admin = _user("aath"), _user("vic"), _user("sarvesh")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    WorkspaceMembership.objects.create(workspace=ws, user=admin, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, turn_mode="auto")
    AgentAdmin.objects.create(agent=agent, user=admin, granted_by=jj)
    return {"ws": ws, "agent": agent, "owner": jj, "editor": editor, "viewer": viewer, "admin": admin}


def _register(client, name="aath-mbp"):
    res = client.post("/api/private-runners/", data={"name": name, "host": "aath.local"},
                      content_type="application/json")
    assert res.status_code in (200, 201), res.content
    return res.json()["id"]


def _open(client, ws_id, session="sess-1", **extra):
    return client.post("/api/harness/local-turns/", data={
        "agent": "ace", "private_runner": ws_id, "claude_session_id": session, "cwd": "/w", **extra,
    }, content_type="application/json")


# --- private runners ----------------------------------------------------------------

def test_registering_is_idempotent_per_person_and_name(world):
    c = _client(world["editor"])
    first = c.post("/api/private-runners/", data={"name": "aath-mbp"}, content_type="application/json")
    again = c.post("/api/private-runners/", data={"name": "aath-mbp"}, content_type="application/json")
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert [w["name"] for w in c.get("/api/private-runners/").json()] == ["aath-mbp"]


def test_a_private_runner_is_its_owners_alone(world):
    ws_id = _register(_client(world["editor"]))
    other = _client(world["admin"])
    assert other.get("/api/private-runners/").json() == []
    # Someone else's private runner cannot carry your direct turn.
    assert _open(other, ws_id).status_code == 404


# --- opening a direct turn --------------------------------------------------------

def test_an_editor_opens_a_running_manual_local_turn_with_an_envelope(world):
    c = _client(world["editor"])
    res = _open(c, _register(c))
    assert res.status_code == 201, res.content
    body = res.json()
    turn = Turn.objects.get(pk=body["turn_id"])
    assert turn.origin == Turn.ORIGIN_LOCAL and turn.status == Turn.RUNNING
    assert turn.claimed_by_id is None and turn.private_runner.name == "aath-mbp"
    assert turn.initiator_user == world["editor"]
    # The agent's own switch is auto; an editor's direct turn is manual anyway.
    assert body["turn_mode"] == "manual"
    env = body["envelope"]
    assert env["turn_mode"]["mode"] == "manual"
    assert env["relationship"] == "member" and env["profile"] == "full"
    assert env["trigger"]["origin"] == "local" and env["trigger"]["private_runner"] == "aath-mbp"
    assert env["trigger"]["runner"] is None


def test_a_resumed_session_reopens_the_same_turn(world):
    c = _client(world["editor"])
    ws_id = _register(c)
    first = _open(c, ws_id).json()["turn_id"]
    again = _open(c, ws_id)
    assert again.status_code == 200 and again.json()["turn_id"] == first
    assert Turn.objects.filter(origin=Turn.ORIGIN_LOCAL).count() == 1


def test_a_closed_sessions_next_life_gets_a_new_turn(world):
    c = _client(world["editor"])
    ws_id = _register(c)
    first = _open(c, ws_id).json()["turn_id"]
    assert c.post(f"/api/harness/turns/{first}/finish", data={"status": "done"},
                  content_type="application/json").status_code == 200
    second = _open(c, ws_id)
    assert second.status_code == 201 and second.json()["turn_id"] != first


def test_a_viewer_is_refused_and_told_why(world):
    c = _client(world["viewer"])
    res = _open(c, _register(c))
    assert res.status_code == 403
    assert "directly" in res.json()["detail"]
    assert not Turn.objects.filter(origin=Turn.ORIGIN_LOCAL).exists()


def test_a_non_member_gets_the_missing_agent_404(world):
    stranger = _user("stranger")
    c = _client(stranger)
    assert _open(c, _register(c)).status_code == 404


@pytest.mark.parametrize("who, asked, mode", [
    ("admin", "auto", "auto"),
    ("admin", "", "manual"),
    ("owner", "auto", "auto"),
    ("editor", "auto", "manual"),
])
def test_only_an_agent_admin_who_asks_runs_auto(world, who, asked, mode):
    c = _client(world[who])
    res = _open(c, _register(c), turn_mode=asked)
    assert res.status_code == 201, res.content
    assert res.json()["turn_mode"] == mode
    assert res.json()["envelope"]["turn_mode"]["mode"] == mode


# --- reporting: the initiator, and nobody else -----------------------------------

def test_the_initiator_finishes_and_uploads_the_transcript(world):
    c = _client(world["editor"])
    tid = _open(c, _register(c)).json()["turn_id"]
    up = c.post(f"/api/harness/turns/{tid}/transcript",
                data={"lines": ['{"type":"user"}', '{"type":"assistant"}'], "batch_id": "b1"},
                content_type="application/json")
    assert up.status_code == 200 and up.json()["line_count"] == 2
    done = c.post(f"/api/harness/turns/{tid}/finish", data={"status": "done"},
                  content_type="application/json")
    assert done.status_code == 200 and done.json()["status"] == "done"


def test_another_editor_cannot_report_on_someones_local_turn(world):
    c = _client(world["editor"])
    tid = _open(c, _register(c)).json()["turn_id"]
    other = _client(world["admin"])
    for path, data in ((f"/api/harness/turns/{tid}/finish", {"status": "failed"}),
                       (f"/api/harness/turns/{tid}/transcript", {"lines": ["{}"]}),
                       (f"/api/harness/local-turns/{tid}/renew", {})):
        assert other.post(path, data=data, content_type="application/json").status_code == 404, path


def test_renew_pushes_the_lease_out(world):
    c = _client(world["editor"])
    tid = _open(c, _register(c)).json()["turn_id"]
    Turn.objects.filter(pk=tid).update(lease_expires_at=timezone.now())
    res = c.post(f"/api/harness/local-turns/{tid}/renew")
    assert res.status_code == 200
    assert Turn.objects.get(pk=tid).lease_expires_at > timezone.now() + timezone.timedelta(hours=7)


# --- a direct turn never holds the agent ------------------------------------------

def test_local_turns_do_not_serialize_the_agent(world):
    """Two people using ACE directly at once, while ACE's runner works a turn."""
    a, b = _client(world["editor"]), _client(world["admin"])
    assert _open(a, _register(a)).status_code == 201
    assert _open(b, _register(b, "sarvesh-mbp")).status_code == 201
    runner = Runner.objects.create(name="box", kind=Runner.EMDASH, owner=world["owner"],
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    # The constraint itself admits the runner's turn beside both local ones.
    with transaction.atomic():
        Turn.objects.create(agent=world["agent"], origin="api", idempotency_key="r1",
                            status=Turn.CLAIMED, claimed_by=runner)
    assert world["agent"].pk in set(routing.busy_agent_ids().values_list("agent_id", flat=True))


def test_a_local_turn_alone_leaves_the_agent_free(world):
    c = _client(world["editor"])
    _open(c, _register(c))
    assert world["agent"].pk not in set(routing.busy_agent_ids().values_list("agent_id", flat=True))
    queued = Turn.objects.create(agent=world["agent"], origin="api", idempotency_key="q1")
    assert routing.blocking_turn(queued) is None


def test_two_runner_turns_still_cannot_execute_at_once(world):
    """The exclusion is for direct turns only — the agent's own lane is unchanged."""
    Turn.objects.create(agent=world["agent"], origin="api", idempotency_key="r1", status=Turn.RUNNING)
    with pytest.raises(IntegrityError), transaction.atomic():
        Turn.objects.create(agent=world["agent"], origin="api", idempotency_key="r2",
                            status=Turn.RUNNING)


def test_local_is_not_a_postable_origin(world):
    c = _client(world["owner"])
    res = c.post("/api/harness/turns/", data={"agent": "ace", "origin": "local",
                                             "idempotency_key": "x", "prompt": "hi"},
                 content_type="application/json")
    assert res.status_code == 422


def test_private_runner_rows_exist_only_for_their_owner(world):
    _register(_client(world["editor"]))
    assert PrivateRunner.objects.get().owner == world["editor"]
