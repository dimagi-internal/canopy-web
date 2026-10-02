"""Teleport: moving a session onto someone ELSE's runner needs that runner's yes.

A transfer spends the target box's machine and Claude subscription. Once a
colleague runs an agent on their own laptop (2026-10-02, ACE on a second
person's box), "can write to this session" stopped being the right question for
"may move it onto that box" — so a move onto a box the requester does not
administer waits for its administrator, and one onto your own box just happens.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant, TeleportRequest
from apps.harness.models import Runner, RunnerAdmin, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SESSION_CAPABLE = {"sessions": True, "projects": ["ace"]}


@pytest.fixture
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    st = User.objects.create_user("st", "stewari@dimagi.com", "pw")
    ace = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    bystander = User.objects.create_user("by", "by@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (st, ace, bystander):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    jj_box = Runner.objects.create(name="jj-mbp-cdp", workspace=ws, status=Runner.ONLINE,
                                   paired_by=jj, host="jj@mbp", capabilities=SESSION_CAPABLE)
    # Paired under the agent's identity; Sarvesh administers it through a grant.
    st_box = Runner.objects.create(name="sarveshtewari-mbp-cdp", workspace=ws,
                                   status=Runner.ONLINE, paired_by=ace, host="st@mbp",
                                   capabilities=SESSION_CAPABLE)
    RunnerAdmin.objects.create(runner=st_box, user=st, granted_by=ace)
    s = Session.objects.create(workspace=ws, project="ace", title="partner thread", created_by=jj)
    RunnerBinding.objects.create(session=s, runner=jj_box, session_key="a-task",
                                 emdash_project="ace", host=jj_box.host, thread_key=str(s.id))
    return {"jj": jj, "st": st, "bystander": bystander, "jj_box": jj_box, "st_box": st_box,
            "session": s}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _ask(user, session, runner, brief="pick up the partner thread"):
    return _as(user).post(f"/api/canopy-sessions/{session.id}/transfer",
                          data={"runner": runner, "brief": brief}, content_type="application/json")


def _bound_to(session) -> str:
    return RunnerBinding.objects.get(session=session).runner.name


def test_a_move_onto_someone_elses_box_waits_and_their_approval_carries_it_out(world):
    res = _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp")  # by NAME
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["status"] == "pending" and body["turn_id"] == ""
    assert "stewari@dimagi.com" in body["approvers"]
    assert _bound_to(world["session"]) == "jj-mbp-cdp"   # nothing moved yet

    # It shows up for the approver.
    waiting = _as(world["st"]).get("/api/canopy-sessions/teleport-requests").json()
    assert [r["id"] for r in waiting] == [body["request_id"]]

    ok = _as(world["st"]).post(f"/api/canopy-sessions/teleport-requests/{body['request_id']}/approve")
    assert ok.status_code == 200, ok.content
    assert ok.json()["status"] == "approved"
    assert ok.json()["transfer"]["runner"] == "sarveshtewari-mbp-cdp"
    assert _bound_to(world["session"]) == "sarveshtewari-mbp-cdp"
    turn = Turn.objects.get(pk=ok.json()["transfer"]["turn_id"])
    assert turn.pinned_runner_id == world["st_box"].id
    assert "pick up the partner thread" in turn.prompt


def test_only_the_targets_administrator_can_approve(world):
    req = _ask(world["jj"], world["session"], str(world["st_box"].id)).json()
    # The requester can't approve their own ask onto a box they don't run…
    r = _as(world["jj"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/approve")
    assert r.status_code == 403
    # …and a bystander can't even see it.
    r = _as(world["bystander"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/approve")
    assert r.status_code == 404
    assert _bound_to(world["session"]) == "jj-mbp-cdp"


def test_admin_is_rechecked_at_decision_time(world):
    req = _ask(world["jj"], world["session"], str(world["st_box"].id)).json()
    RunnerAdmin.objects.filter(user=world["st"]).delete()
    r = _as(world["st"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/approve")
    assert r.status_code in (403, 404)
    assert _bound_to(world["session"]) == "jj-mbp-cdp"


def test_a_move_onto_your_own_box_happens_now(world):
    # A thread Jonathan started that Sarvesh is in; he pulls it onto his own box.
    SessionParticipant.objects.create(session=world["session"], user=world["st"],
                                      role=SessionParticipant.EDITOR)
    res = _ask(world["st"], world["session"], "sarveshtewari-mbp-cdp")
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "moved"
    assert res.json()["transferred_from"] == "jj-mbp-cdp"
    assert _bound_to(world["session"]) == "sarveshtewari-mbp-cdp"


def test_decline_leaves_the_session_where_it_is(world):
    req = _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()
    r = _as(world["st"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/decline",
                              data={"note": "mid-demo"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "declined" and r.json()["note"] == "mid-demo"
    assert _bound_to(world["session"]) == "jj-mbp-cdp"
    assert not Turn.objects.filter(chat_session=world["session"]).exists()


def test_a_second_pending_request_is_refused(world):
    assert _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()["status"] == "pending"
    assert _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").status_code == 409


def test_the_requester_can_cancel_and_ask_again(world):
    req = _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()
    r = _as(world["jj"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()["status"] == "pending"


def test_an_unanswered_request_expires(world):
    req = _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()
    TeleportRequest.objects.filter(pk=req["request_id"]).update(
        created_at=TeleportRequest.objects.get(pk=req["request_id"]).created_at
        - __import__("datetime").timedelta(hours=25))
    r = _as(world["st"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/approve")
    assert r.status_code == 422 and "expired" in r.json()["detail"]
    assert _bound_to(world["session"]) == "jj-mbp-cdp"


def test_a_slack_born_session_hears_about_it(world, django_capture_on_commit_callbacks):
    from apps.slack import relay

    with mock.patch.object(relay, "notify_teleport", return_value=True) as notify:
        with django_capture_on_commit_callbacks(execute=True):
            req = _ask(world["jj"], world["session"], "sarveshtewari-mbp-cdp").json()
        with django_capture_on_commit_callbacks(execute=True):
            _as(world["st"]).post(f"/api/canopy-sessions/teleport-requests/{req['request_id']}/approve")
    assert [c.args[0].status for c in notify.call_args_list] == ["pending", "approved"]
    assert "asked to move this conversation to *sarveshtewari-mbp-cdp*" in relay.teleport_text(
        TeleportRequest(session=world["session"], to_runner=world["st_box"],
                        requested_by=world["jj"], status="pending"))


def test_a_move_between_two_boxes_of_the_same_owner_never_asks(world):
    """Jonathan's two macOS accounts (ada's user-switch): same owner, same
    subscription holder — even a collaborator moving it needs no one's yes."""
    jj2 = Runner.objects.create(name="jj-other-account", workspace=world["jj_box"].workspace,
                                status=Runner.ONLINE, paired_by=world["jj"], host="jj2@mbp",
                                capabilities=SESSION_CAPABLE)
    SessionParticipant.objects.create(session=world["session"], user=world["st"],
                                      role=SessionParticipant.EDITOR)
    res = _ask(world["st"], world["session"], jj2.name)
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "moved"
    assert _bound_to(world["session"]) == "jj-other-account"
    assert "same owner" in TeleportRequest.objects.get(pk=res.json()["request_id"]).note
