"""The supervisor feed and the pushes agree, because they ask one rule.

`apps/canopy_sessions/feed.py` decides whether a session is on a person's feed;
the session list stamps it as `feed_status` and every session push sends only
to someone it is on the feed for. Pinned against the case that motivated it
(Jonathan, 2026-10-05): a colleague's laptop session showed on his feed and
buzzed his phone, though it was the colleague's to answer.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import feed
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.push import services as push_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

MENU = {
    "question": "Proceed?",
    "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}],
    "source": "transcript",
}


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


@pytest.fixture()
def fleet():
    """jj owns the workspace (so he is an ownerless agent's audience); sarvesh
    is an editor with his own laptop runner."""
    jj, sarvesh = _user("jj"), _user("sarvesh")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=sarvesh, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=None)
    runners = {
        u.username: Runner.objects.create(
            name=f"{u.username}-mbp", kind=Runner.EMDASH, host=f"{u.username}-mac", owner=u,
            workspace=ws, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        )
        for u in (jj, sarvesh)
    }
    return {"jj": jj, "sarvesh": sarvesh, "ws": ws, "agent": agent, "runners": runners}


def _blocked_session(fleet, *, on, created_by=None, key="spark"):
    """A session blocked on a dialog — waiting on somebody — on `on`'s runner."""
    session = Session.objects.create(
        workspace=fleet["ws"], agent=fleet["agent"], title=key, created_by=created_by,
        origin=Session.ORIGIN_RUNNER if created_by is None else Session.ORIGIN_WEB,
    )
    RunnerBinding.objects.create(
        session=session, runner=fleet["runners"][on], session_key=key,
        live_seen_at=timezone.now(), pending_question=MENU,
    )
    return Session.objects.select_related("runner_binding__runner").get(pk=session.pk)


def _feed_status(user, session) -> str:
    c = Client()
    c.force_login(user)
    rows = c.get("/api/canopy-sessions/?reply=true").json()
    return next((r["feed_status"] for r in rows if r["id"] == str(session.id)), None)


def _pushed_to(monkeypatch, session) -> list[str]:
    sent = []
    monkeypatch.setattr(push_services, "send_to_user", lambda user, **kw: sent.append(user.username) or 1)
    push_services.notify_session_question(session, MENU)
    return sent


def test_a_colleagues_runner_session_is_on_their_feed_not_yours(fleet, monkeypatch):
    session = _blocked_session(fleet, on="sarvesh")
    assert _feed_status(fleet["jj"], session) == feed.NOT_YOURS
    assert _feed_status(fleet["sarvesh"], session) == feed.WAITING
    # The push skips jj (a workspace owner, so the ownerless agent's audience)
    # and reaches the person whose feed it is on.
    assert _pushed_to(monkeypatch, session) == ["sarvesh"]


def test_a_chat_you_started_stays_yours_whatever_runner_took_it(fleet, monkeypatch):
    session = _blocked_session(fleet, on="sarvesh", created_by=fleet["jj"], key="mine")
    assert _feed_status(fleet["jj"], session) == feed.WAITING
    assert _pushed_to(monkeypatch, session) == ["jj"]


def test_a_session_on_your_own_runner_is_yours(fleet, monkeypatch):
    session = _blocked_session(fleet, on="jj")
    assert _feed_status(fleet["jj"], session) == feed.WAITING
    assert _pushed_to(monkeypatch, session) == ["jj"]


def test_a_paused_runner_parks_it_and_nobody_is_pushed(fleet, monkeypatch):
    runner = fleet["runners"]["jj"]
    runner.paused = True
    runner.save(update_fields=["paused"])
    session = _blocked_session(fleet, on="jj")
    assert _feed_status(fleet["jj"], session) == feed.PARKED
    assert _pushed_to(monkeypatch, session) == []


def test_an_agents_own_auto_run_is_held_and_nobody_is_pushed(fleet, monkeypatch):
    session = _blocked_session(fleet, on="jj")
    monkeypatch.setattr(feed, "driving_turn", lambda s: ("auto", "canopy_scheduler"))
    assert _pushed_to(monkeypatch, session) == []
    assert feed.held(fleet["jj"], session, turn_mode="auto", turn_origin="canopy_scheduler") == feed.AUTO
    # A chat you are having stays yours even when its turn ran auto.
    assert feed.held(fleet["jj"], session, turn_mode="auto", turn_origin="canopy_web_chat") == ""


def test_not_waiting_is_on_nobodys_feed(fleet):
    session = _blocked_session(fleet, on="jj")
    RunnerBinding.objects.filter(session=session).update(pending_question=None)
    assert _feed_status(fleet["jj"], session) == ""


def test_an_agents_cloud_session_is_on_its_owners_feed(fleet, monkeypatch):
    """Nobody sits at a cloud box, so a manual session an agent opened there — a
    dispatch waiting for approval — is the agent owner's (Jonathan, 2026-10-07).
    Before, it was NOT_YOURS for everyone and reached no one's feed."""
    agent = fleet["agent"]
    agent.owner = fleet["jj"]
    agent.save(update_fields=["owner"])
    fleet["runners"]["cloud"] = Runner.objects.create(
        name="cloud-ec2-2", kind=Runner.CLOUD, host="ec2", owner=fleet["sarvesh"],
        workspace=fleet["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    session = _blocked_session(fleet, on="cloud", key="dispatch")
    monkeypatch.setattr(feed, "driving_turn", lambda s: ("manual", "api"))
    assert _feed_status(fleet["jj"], session) == feed.WAITING
    assert _pushed_to(monkeypatch, session) == ["jj"]


def test_every_session_row_carries_its_turn_mode(fleet):
    """The sessions list labels each card with its mode, not only the feed."""
    from apps.harness.models import Turn

    session = _blocked_session(fleet, on="jj")
    Turn.objects.create(agent=fleet["agent"], origin="canopy_scheduler", idempotency_key="t1",
                        status=Turn.DONE, session_key="spark", turn_mode="auto",
                        claimed_by=fleet["runners"]["jj"])
    c = Client()
    c.force_login(fleet["jj"])
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(session.id))
    assert row["turn_mode"] == "auto"
