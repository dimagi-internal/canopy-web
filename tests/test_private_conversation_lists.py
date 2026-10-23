"""A private (sensitive) conversation in a list: what it is and who started it, never
what it says; and asking to be added to one you can see but are not in.

Spec: docs/superpowers/specs/2026-10-23-sensitive-conversations-sealed-to-their-people-design.md.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import AgentAdmin
from apps.canopy_sessions import join_requests
from apps.canopy_sessions.models import JoinRequest, Session, SessionParticipant
from apps.realtime.groups import session_group
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import ASKED, REPLY, ROW, _converse, rewrap_for, vault_key, world

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    w = world()
    w["user"].first_name, w["user"].last_name = "Jonathan", "Jackson"
    w["user"].save()
    # The agent's own conversation, which its admins can list (`visible_session_q` leg 4).
    w["hot"].origin = Session.ORIGIN_RUNNER
    w["hot"].save(update_fields=["origin"])
    admin = User.objects.create_user("ada", "ada@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=admin, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w["agent"], user=admin)
    w["admin"] = admin
    w["admin_key"] = vault_key(admin)
    w["ac"] = Client()
    w["ac"].force_login(admin)
    return w


def _row(client, session):
    rows = client.get("/api/canopy-sessions/?limit=50&state=all").json()
    return next(r for r in rows if r["id"] == str(session.pk))


def test_a_private_row_says_what_it_is_and_who_started_it_and_nothing_it_said(w):
    _converse(w["hot"])
    for client, in_it in ((w["c"], True), (w["ac"], False)):
        row = _row(client, w["hot"])
        assert row["sensitive"] is True and row["you_are_in_it"] is in_it
        assert row["started_by_name"] == "Jonathan Jackson"
        assert row["title"] == "" and row["opening"] == "" and row["last_reply"] == ""
        assert ASKED not in str(row) and REPLY not in str(row) and ROW not in str(row)
    cold = _row(w["c"], w["cold"])
    assert cold["you_are_in_it"] is None and cold["title"] == "cold"


def test_the_read_says_whether_you_are_in_it(w):
    mine = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert mine["you_are_in_it"] is True and mine["your_join_request"] == ""
    theirs = w["ac"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert theirs["you_are_in_it"] is False and theirs["sealed_key"] is None


def test_home_lists_it_as_private():
    from apps.canopy_sessions import attention

    assert attention.private_title({"started_by_name": "Jonathan Jackson"}) == \
        "Private conversation · Jonathan Jackson"
    assert attention._title({"sensitive": True, "started_by_name": "", "title": "x", "opening": "y"}) == \
        "Private conversation"


def test_someone_who_can_see_it_asks_and_the_request_carries_nothing_it_said(w):
    _converse(w["hot"])
    r = w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "pending" and body["requested_by"] == "ada@dimagi.com"
    assert set(body) == {"id", "session_id", "requested_by", "requested_by_name", "status",
                         "decided_by", "decided_at", "created_at"}
    assert ASKED not in str(body) and REPLY not in str(body)
    # Asking again is the same request.
    assert w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()["id"] == body["id"]
    assert w["ac"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["your_join_request"] == "pending"
    # Its owner sees it; the asker sees no one else's.
    listed = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    assert [x["id"] for x in listed] == [body["id"]]
    assert w["ac"].get(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json() == []


def test_the_request_goes_to_the_conversation_and_never_to_home(w):
    w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests")
    for client in (w["c"], w["ac"]):
        data = client.get("/api/me/attention/").json()
        rows = data["now"] + data["quiet"] + data["hidden"] + data["running"]
        assert not [r for r in rows if r["kind"] == "join_request"]


def test_its_people_see_the_request_and_only_its_owner_answers(w):
    """Anyone in it sees who is asking; only its owner (who can wrap them a key) decides."""
    req = join_requests.ask(w["hot"], w["admin"])
    beth = User.objects.create_user("beth", "beth@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=beth, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=w["hot"], user=beth, role=SessionParticipant.EDITOR)
    bc = Client()
    bc.force_login(beth)
    listed = bc.get(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    assert [x["id"] for x in listed] == [str(req.pk)]
    # Not hers to answer: she cannot even name it to decline it.
    assert bc.post(f"/api/canopy-sessions/join-requests/{req.pk}/decline").status_code == 404
    req.refresh_from_db()
    assert req.status == JoinRequest.PENDING


def test_every_change_rings_the_conversation_with_the_whole_open_list(
        w, monkeypatch, django_capture_on_commit_callbacks):
    sent = []
    monkeypatch.setattr(join_requests, "_publish", lambda group, msg: sent.append((group, msg)))
    _converse(w["hot"])
    with django_capture_on_commit_callbacks(execute=True):
        req = w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    [(group, msg)] = sent
    assert group == session_group(w["hot"].pk)
    assert msg["type"] == "session.join_requests"
    assert [r["id"] for r in msg["requests"]] == [req["id"]]
    assert msg["requests"][0]["requested_by"] == "ada@dimagi.com"
    assert ASKED not in str(msg) and REPLY not in str(msg)
    # Asking again changes nothing, so rings nothing.
    sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests")
    assert sent == []
    # Adding them answers it: the list empties, live.
    with django_capture_on_commit_callbacks(execute=True):
        w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/participants", data={
            "email": "ada@dimagi.com", "role": "editor",
            "wrap": rewrap_for(w["hot"], w["user"], w["key"], w["admin"])}, content_type="application/json")
    assert sent[-1][1]["requests"] == []
    # A decline or a withdrawal rings it too.
    other = join_requests.ask(w["hot"], User.objects.create_user("z", "z@dimagi.com", "pw"))
    sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        join_requests.decline(other, w["user"])
    assert sent and sent[-1][1]["requests"] == []


def test_the_socket_frame_reaches_ag_ui_clients():
    from apps.canopy_sessions import agui

    [event] = agui.project({"event": "session.join_requests", "data": {"requests": [{"id": "r1"}]}},
                           thread_id="t1")
    assert event.name == "canopy.session.join_requests" and event.value["requests"] == [{"id": "r1"}]


def test_adding_them_from_the_owners_browser_approves_it(w):
    _converse(w["hot"])
    req = w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    r = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/participants", data={
        "email": "ada@dimagi.com", "role": "editor",
        "wrap": rewrap_for(w["hot"], w["user"], w["key"], w["admin"])}, content_type="application/json")
    assert r.status_code == 200, r.content
    done = JoinRequest.objects.get(pk=req["id"])
    assert done.status == JoinRequest.APPROVED and done.decided_by == w["user"]
    assert w["ac"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["you_are_in_it"] is True


def test_only_its_owner_in_it_declines_and_only_the_asker_withdraws(w):
    req = w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    # The asker cannot decide their own request.
    assert w["ac"].post(f"/api/canopy-sessions/join-requests/{req['id']}/decline").status_code == 403
    assert w["c"].post(f"/api/canopy-sessions/join-requests/{req['id']}/cancel").status_code == 403
    r = w["c"].post(f"/api/canopy-sessions/join-requests/{req['id']}/decline")
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert w["c"].post(f"/api/canopy-sessions/join-requests/{req['id']}/decline").status_code == 409
    assert w["ac"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["your_join_request"] == "declined"
    again = w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").json()
    assert again["status"] == "pending" and again["id"] != req["id"]
    assert w["ac"].post(f"/api/canopy-sessions/join-requests/{again['id']}/cancel").json()["status"] == \
        "cancelled"


def test_there_is_nothing_to_ask_of_an_ordinary_conversation_or_one_you_are_in(w):
    assert w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").status_code == 409
    assert w["c"].post(f"/api/canopy-sessions/{w['cold'].pk}/join-requests").status_code == 409
    # Nobody in a conversation no member started can add you.
    w["hot"].created_by = None
    w["hot"].save(update_fields=["created_by"])
    assert w["ac"].post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").status_code == 409


def test_someone_who_cannot_see_it_cannot_ask(w):
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=stranger, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(stranger)
    assert c.post(f"/api/canopy-sessions/{w['hot'].pk}/join-requests").status_code == 404
    assert not JoinRequest.objects.exists()


def test_a_participant_owner_decides_too(w):
    req = join_requests.ask(w["hot"], w["admin"])
    assert join_requests.may_decide(w["user"], w["hot"])
    assert not join_requests.may_decide(w["admin"], w["hot"])
    SessionParticipant.objects.create(session=w["hot"], user=w["admin"], role=SessionParticipant.EDITOR)
    assert join_requests.settle_on_add(w["hot"], w["admin"], by=w["user"]) == 1
    req.refresh_from_db()
    assert req.status == JoinRequest.APPROVED
