"""The sessions open on a runner, for its owner and admins — facts, never words.

`GET /runners/{id}/sessions/` lists every session the runner's `open_sessions`
counts, including people's web chats the admin cannot open; it says who started
each and why it is open, and carries no title, prompt or message. `POST
…/sessions/{sid}/close` closes an idle one and refuses one in use.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, RunnerAdmin, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def box():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan",
                                     last_name="Jackson")
    admin = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    member = User.objects.create_user("m", "m@dimagi.com", "pw")
    ace_login = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    for u, role in ((owner, WorkspaceMembership.OWNER), (admin, WorkspaceMembership.EDITOR),
                    (member, WorkspaceMembership.EDITOR), (ace_login, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, user=ace_login)
    cloud = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.CLOUD, workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), owner=owner, host="cloud-ec2-1",
        capabilities={"sessions": True},
    )
    RunnerAdmin.objects.create(runner=cloud, user=admin, granted_by=owner)
    return {"owner": owner, "admin": admin, "member": member, "ace_login": ace_login,
            "ws": ws, "ace": ace, "cloud": cloud}


def _chat(box, title, starter, *, status=Turn.DONE, question=None):
    """A web chat on the cloud box, started by `starter` (a person, or an agent's login)."""
    session = Session.objects.create(workspace=box["ws"], agent=box["ace"], created_by=starter,
                                     title=title, origin=Session.ORIGIN_WEB)
    RunnerBinding.objects.create(session=session, runner=box["cloud"], session_key=title,
                                 live_seen_at=timezone.now() - dt.timedelta(hours=5),
                                 pending_question=question)
    Turn.objects.create(
        agent=box["ace"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, idempotency_key=title,
        status=status, session_key=title, turn_mode="auto", canopy_session=session,
        claimed_by=box["cloud"], prompt=f"secret words in {title}",
        initiator_kind="user", initiator_via="chat", initiator_user=starter,
    )
    return session


def _as(user):
    c = Client()
    c.force_login(user)
    return c


def _url(box, suffix=""):
    return f"/api/w/w1/harness/runners/{box['cloud'].pk}/sessions/{suffix}"


def test_an_admin_sees_every_open_session_with_who_started_it_and_no_words(box):
    mine = _chat(box, "jj-testing-something", box["owner"])
    # Running, so it is open: an agent-started chat closes once nothing runs on it.
    _chat(box, "ace-at-work", box["ace_login"], status=Turn.RUNNING)

    # The admin cannot open the owner's chat …
    assert _as(box["admin"]).get(f"/api/w/w1/sessions/{mine.pk}").status_code == 404
    # … but sees that it exists, whose it is, and why it is open.
    resp = _as(box["admin"]).get(_url(box))
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()}
    assert len(rows) == 2
    row = rows[str(mine.pk)]
    assert row["started_by"] == {"kind": "user", "agent": "", "email": "jj@dimagi.com",
                                 "name": "Jonathan Jackson"}
    assert row["open_reason"] == "person_started"
    assert row["first_turn_origin"] == Turn.ORIGIN_CANOPY_WEB_CHAT
    assert row["turns"] == 1
    agent_row = next(r for r in rows.values() if r["id"] != str(mine.pk))
    assert agent_row["started_by"]["kind"] == "agent"
    assert agent_row["started_by"]["agent"] == "ace"

    body = resp.content.decode()
    assert "jj-testing-something" not in body and "secret words" not in body
    assert "title" not in row


def test_only_the_boxs_owner_and_admins_can_list_it(box):
    _chat(box, "a", box["owner"])
    assert _as(box["owner"]).get(_url(box)).status_code == 200
    assert _as(box["member"]).get(_url(box)).status_code == 404


def test_an_admin_closes_an_idle_session_but_not_one_in_use(box):
    idle = _chat(box, "idle", box["owner"])
    running = _chat(box, "running", box["owner"], status=Turn.RUNNING)
    asking = _chat(box, "asking", box["owner"],
                   question={"question": "Proceed?", "options": [{"label": "Yes"}]})
    admin = _as(box["admin"])

    assert admin.post(_url(box, f"{idle.pk}/close")).json() == {
        "ok": True, "closing": False, "reason": ""}
    idle.refresh_from_db()
    assert idle.status == Session.ARCHIVED

    assert admin.post(_url(box, f"{running.pk}/close")).json()["reason"] == "in_use:turn_running"
    assert admin.post(_url(box, f"{asking.pk}/close")).json()["reason"] == "in_use:dialog_up"
    assert admin.post(_url(box, f"{idle.pk}/close")).json()["reason"] == "not_open"
    for s in (running, asking):
        s.refresh_from_db()
        assert s.status == Session.ACTIVE

    assert _as(box["member"]).post(_url(box, f"{running.pk}/close")).status_code == 404
