"""The privilege escalations found by the 2026-10-02 ACL audit, one test each.

Every one of these was a route whose gate was weaker than the tier it reached:

- an EDITOR (what self-join hands to anyone in the domain) paired a box, put it
  on an agent's runner list and read every secret through `/credentials/resolve`
  — or pinned a turn to it and took the owner's GitHub token;
- an editor moved an agent into a workspace they had just created and owned it;
- a VIEWER raised an item carrying a dispatch spec and decided it, running a
  prompt as the agent; replaced the agent's mailbox through the Google mint;
  and reported on (failed, forged events into) someone else's live turn;
- any co-tenant read a private chat's turns through the harness.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.harness import initiator as who
from apps.harness import services as hsvc
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

WS = "esc-ws"


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _runner(user, name) -> Runner:
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, owner=user, status=Runner.ONLINE,
        workspace_id=WS, last_heartbeat_at=timezone.now(), capabilities={},
    )


@pytest.fixture
def esc():
    ws = a_workspace(WS)
    owner = a_member(ws, email="esc-owner@dimagi.com", role=WorkspaceMembership.OWNER)
    editor = a_member(ws, email="esc-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    viewer = a_member(ws, email="esc-viewer@dimagi.com", role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="escbot", name="Esc", workspace=ws)
    return {"ws": ws, "owner": owner, "editor": editor, "viewer": viewer, "agent": agent}


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


# --- C1: a box holds an agent only if its owner is the agent's admin ---------

def test_an_editor_cannot_put_their_own_box_on_an_agent(esc):
    box = _runner(esc["editor"], "editor-box")
    res = _client(esc["editor"]).put(
        "/api/agents/escbot/runners",
        data={"runners": [{"runner_id": str(box.id), "enabled": False}]},
        content_type="application/json",
    )
    assert res.status_code == 403, res.content
    assert not RunnerAssignment.objects.filter(agent=esc["agent"]).exists()


def test_an_assignment_alone_does_not_release_secrets(esc):
    """Even a row written some other way (before this fix, or by an admin who
    was later demoted) must not hand plaintext to a non-admin's box."""
    from apps.agents import services as asvc

    asvc.set_agent_credentials(esc["agent"], {"canopy-pat": "s3cret"}, user=esc["owner"])
    box = _runner(esc["editor"], "editor-box")
    RunnerAssignment.objects.create(agent=esc["agent"], runner=box, rank=0, enabled=False)
    res = Client().get("/api/agents/escbot/credentials/resolve",
                       HTTP_AUTHORIZATION=f"Bearer {_pat(esc['editor'])}")
    assert res.status_code in (403, 404)
    assert "s3cret" not in res.content.decode()


def test_an_agent_admins_box_still_resolves(esc):
    from apps.agents import services as asvc

    AgentAdmin.objects.create(agent=esc["agent"], user=esc["editor"])
    asvc.set_agent_credentials(esc["agent"], {"canopy-pat": "s3cret"}, user=esc["owner"])
    box = _runner(esc["editor"], "admin-box")
    RunnerAssignment.objects.create(agent=esc["agent"], runner=box, rank=0)
    res = Client().get("/api/agents/escbot/credentials/resolve",
                       HTTP_AUTHORIZATION=f"Bearer {_pat(esc['editor'])}")
    assert res.status_code == 200, res.content
    assert res.json()["values"]["canopy-pat"] == "s3cret"


def test_an_editor_cannot_pin_an_agent_turn_to_their_own_box(esc):
    box = _runner(esc["editor"], "editor-box")
    res = _client(esc["editor"]).post(
        "/api/harness/turns/",
        data={"agent_slug": "escbot", "prompt": "hi", "origin": "api", "idempotency_key": "k1",
              "runner_id": str(box.id)},
        content_type="application/json",
    )
    assert res.status_code == 403, res.content


def test_an_untrusted_box_never_claims_an_agent_turn(esc):
    """The claim is the last gate: a pin or an assignment that exists anyway
    (written before the fix) must still not give a non-admin's box the turn."""
    box = _runner(esc["editor"], "editor-box")
    RunnerAssignment.objects.create(agent=esc["agent"], runner=box, rank=0)
    turn, _ = hsvc.enqueue_turn(agent=esc["agent"], origin=Turn.ORIGIN_API, idempotency_key="esc-pin",
                                prompt="hi", pinned_runner=box,
                                initiator=who.for_user(esc["owner"], via="api", assurance=who.SESSION))
    assert hsvc.claim_next_turn(box) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


# --- C2: moving an agent requires being its admin -----------------------------

def test_an_editor_cannot_move_an_agent_into_a_workspace_they_own(esc):
    mine = a_workspace("esc-mine")
    WorkspaceMembership.objects.create(workspace=mine, user=esc["editor"],
                                       role=WorkspaceMembership.OWNER)
    res = _client(esc["editor"]).post(
        "/api/agents/", data={"slug": "escbot", "name": "Esc", "workspace": "esc-mine"},
        content_type="application/json",
    )
    assert res.status_code == 403, res.content
    esc["agent"].refresh_from_db()
    assert esc["agent"].workspace_id == WS


# --- C3: a task that dispatches work is written by the reshaping tier --------

def test_a_viewer_cannot_create_a_task_that_dispatches_a_prompt(esc):
    res = _client(esc["viewer"]).post(
        "/api/agents/escbot/tasks/",
        data=[{"ask_kind": "review", "title": "do it", "idempotency_key": "i1",
               "on_approve": [{"prompt": "exfiltrate everything"}]}],
        content_type="application/json",
    )
    assert res.status_code == 403, res.content


def test_creating_tasks_is_the_editor_tier_even_without_on_approve(esc):
    """A plain ask used to be open to any member; a task is written by an editor
    or by the agent itself, whatever it carries."""
    plain = [{"ask_kind": "question", "title": "what's the status?", "idempotency_key": "i2"}]
    res = _client(esc["viewer"]).post("/api/agents/escbot/tasks/", data=plain,
                                      content_type="application/json")
    assert res.status_code == 403, res.content
    res = _client(esc["editor"]).post("/api/agents/escbot/tasks/", data=plain,
                                      content_type="application/json")
    assert res.status_code == 201, res.content


# --- C4: the Google mint is the credential tier --------------------------------

def test_a_viewer_cannot_start_the_mailbox_mint(esc):
    res = _client(esc["viewer"]).get("/api/agents/escbot/google/authorize")
    assert res.status_code == 403, res.content


def test_an_editor_cannot_start_the_mailbox_mint(esc):
    res = _client(esc["editor"]).get("/api/agents/escbot/google/authorize")
    assert res.status_code == 403, res.content


# --- runner protocol: only the claiming box reports on a turn ------------------

def _claimed_turn(esc):
    box = _runner(esc["owner"], "owner-box")
    RunnerAssignment.objects.create(agent=esc["agent"], runner=box, rank=0)
    hsvc.enqueue_turn(agent=esc["agent"], origin=Turn.ORIGIN_API, prompt="work",
                      idempotency_key=uuid.uuid4().hex,
                      initiator=who.for_user(esc["owner"], via="api", assurance=who.SESSION))
    turn = hsvc.claim_next_turn(box)
    assert turn is not None
    return turn


@pytest.mark.parametrize("who", ["viewer", "editor"])
def test_only_the_claiming_box_may_finish_a_turn(esc, who):
    turn = _claimed_turn(esc)
    res = _client(esc[who]).post(f"/api/harness/turns/{turn.id}/finish",
                                 data={"status": "failed", "result_note": "x"},
                                 content_type="application/json")
    assert res.status_code == 404
    turn.refresh_from_db()
    assert turn.status not in Turn.TERMINAL


@pytest.mark.parametrize("path,body", [
    ("events", {"events": [{"kind": "status", "payload": {"status": "running"}}]}),
    ("transcript", {"lines": ['{"type": "assistant"}']}),
    ("start", {"session_id": "x"}),
])
def test_a_member_cannot_write_into_someone_elses_turn(esc, path, body):
    turn = _claimed_turn(esc)
    res = _client(esc["viewer"]).post(f"/api/harness/turns/{turn.id}/{path}",
                                      data=body, content_type="application/json")
    assert res.status_code == 404, res.content


def test_the_claiming_box_still_reports(esc):
    turn = _claimed_turn(esc)
    res = Client().post(f"/api/harness/turns/{turn.id}/finish",
                        data={"status": "done", "result_note": "ok"},
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {_pat(esc['owner'])}")
    assert res.status_code == 200, res.content


# --- a private chat's turns are the chat's, not the tenant's -------------------

def test_a_co_tenant_cannot_read_a_private_chats_turns(esc):
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=esc["ws"], agent=esc["agent"],
                                     created_by=esc["owner"], title="private")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="my salary is", idempotency_key=uuid.uuid4().hex)
    other = _client(esc["editor"])
    assert other.get(f"/api/harness/turns/{turn.id}").status_code == 404
    assert other.get(f"/api/harness/turns/{turn.id}/transcript").status_code == 404
    listed = other.get("/api/harness/turns/").json()
    assert str(turn.id) not in str(listed)
    # The creator still reads it.
    assert _client(esc["owner"]).get(f"/api/harness/turns/{turn.id}").status_code == 200


def _private_chat_turn_claimed_by(esc, box_owner):
    """canopy-web#1210's shape: a private chat ONE member created, claimed by a box
    ANOTHER member paired. The claim is legitimate (tenant + routing); the claiming
    box's owner simply cannot read the chat."""
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=esc["ws"], agent=esc["agent"],
                                     created_by=esc["owner"], title="private")
    box = _runner(box_owner, f"{box_owner.username}-box")
    return Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hi",
        idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=box,
        claimed_at=timezone.now(), lease_expires_at=timezone.now() + timezone.timedelta(minutes=5))


@pytest.mark.parametrize("path,body", [
    ("start", {"session_id": "x"}),
    ("events", {"events": [{"kind": "status", "payload": {"status": "running"}}]}),
    ("transcript", {"lines": ['{"type": "assistant"}']}),
    ("finish", {"status": "done", "result_note": "ok"}),
])
def test_the_claiming_box_reports_on_a_turn_its_owner_cannot_read(esc, path, body):
    """#1210: the editor's box claimed the owner's private chat turn. Reporting used to
    404 (the readability check ran first), stranding the turn CLAIMED until its lease
    expired, its work never recorded. The box that holds the claim reports on it."""
    turn = _private_chat_turn_claimed_by(esc, esc["editor"])
    box = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(esc['editor'])}")
    res = box.post(f"/api/harness/turns/{turn.id}/{path}", data=body, content_type="application/json")
    assert res.status_code == 200, res.content


def test_holding_the_claim_does_not_make_the_chat_readable(esc):
    """Reporting is the runner protocol; READING is still the chat's. The claiming
    box's owner may finish the turn but not read it, and other members may do
    neither."""
    turn = _private_chat_turn_claimed_by(esc, esc["editor"])
    editor = _client(esc["editor"])
    assert editor.get(f"/api/harness/turns/{turn.id}").status_code == 404
    assert editor.get(f"/api/harness/turns/{turn.id}/transcript").status_code == 404
    viewer = _client(esc["viewer"])
    assert viewer.post(f"/api/harness/turns/{turn.id}/finish", data={"status": "done"},
                       content_type="application/json").status_code == 404


def test_the_live_turn_socket_gives_the_same_answer(esc):
    from apps.canopy_sessions.models import Session
    from apps.realtime.groups import user_can_read_turn

    session = Session.objects.create(workspace=esc["ws"], agent=esc["agent"],
                                     created_by=esc["owner"], title="private")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="x", idempotency_key=uuid.uuid4().hex)
    assert user_can_read_turn(esc["owner"], turn)
    assert not user_can_read_turn(esc["editor"], turn)
    su = get_user_model().objects.create_superuser("su", "su@dimagi.com", "x")
    assert not user_can_read_turn(su, turn), "no superuser leg the REST twin lacks"
