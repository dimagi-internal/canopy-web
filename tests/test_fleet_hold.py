"""The fleet hold — stop EVERY runner from starting anything, and see what tries.

Asked for on 2026-10-07 when sessions nobody could account for (people-digest turns, since removed)
kept appearing on a laptop: hold the whole fleet, let turns pile up QUEUED with their
triggers, trace them, release. The rules these tests hold:

  ONE GATE. `claim_next_turn` refuses every claim while held — pins and rides included — so it
  binds every runner without a deploy on any box.

  NOTHING IS LOST. Turns still enqueue while held and are claimed after the release.

  SUPERUSER to set or release (it spans every tenant); a NAMED HOLDER
  (`CANOPY_FLEET_HOLDERS`, Ada) may set it but never release it — an agent can stop
  the fleet, not restart it. Anyone signed in may read.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness import claim
from apps.harness.models import FleetHold, Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(superuser=True, email="jj@dimagi.com"):
    user = User.objects.create_user("jj", email, "pw", is_superuser=superuser)
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    runner = Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, workspace=ws, owner=user,
        host="jjackson@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["connect-labs"]},
    )
    return user, ws, c, runner


def _queued(ws, **kw):
    return Turn.objects.create(
        project="connect-labs", workspace=ws, status=Turn.QUEUED, origin="api",
        idempotency_key=f"k-{Turn.objects.count()}", **kw)


def test_a_held_fleet_claims_nothing_and_the_turn_waits():
    _u, ws, c, runner = _ctx()
    turn = _queued(ws)
    assert c.post("/api/harness/fleet-hold", data={"note": "tracing digests"},
                  content_type="application/json").status_code == 200

    assert claim.claim_next_turn(runner) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_a_pin_does_not_punch_through_the_hold():
    _u, ws, c, runner = _ctx()
    _queued(ws, pinned_runner=runner)
    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK, defaults={"held": True})

    assert claim.claim_next_turn(runner) is None


def test_release_lets_the_backlog_through():
    _u, ws, c, runner = _ctx()
    turn = _queued(ws)
    c.post("/api/harness/fleet-hold", data={}, content_type="application/json")
    assert claim.claim_next_turn(runner) is None

    resp = c.post("/api/harness/fleet-hold/release")

    assert resp.status_code == 200 and resp.json()["held"] is False
    claimed = claim.claim_next_turn(runner)
    assert claimed is not None and claimed.pk == turn.pk


def test_the_hold_reports_who_why_and_how_much_is_waiting():
    user, ws, c, _runner = _ctx()
    _queued(ws)
    _queued(ws)

    body = c.post("/api/harness/fleet-hold", data={"note": "tracing digests"},
                  content_type="application/json").json()

    assert body["held"] is True
    assert body["note"] == "tracing digests"
    assert body["held_by_email"] == user.email
    assert body["queued"] == 2
    assert body["can_hold"] is True
    assert c.get("/api/harness/fleet-hold").json()["held"] is True


def test_holding_again_keeps_the_original_time_and_refreshes_the_note():
    _u, _ws, c, _runner = _ctx()
    c.post("/api/harness/fleet-hold", data={"note": "first"}, content_type="application/json")
    first_at = FleetHold.current().held_at

    c.post("/api/harness/fleet-hold", data={"note": "second"}, content_type="application/json")

    hold = FleetHold.current()
    assert hold.note == "second" and hold.held_at == first_at


def test_only_a_superuser_may_hold_or_release():
    _u, _ws, c, _runner = _ctx(superuser=False)

    assert c.post("/api/harness/fleet-hold", data={},
                  content_type="application/json").status_code == 403
    assert c.post("/api/harness/fleet-hold/release").status_code == 403
    body = c.get("/api/harness/fleet-hold").json()
    assert body["held"] is False and body["can_hold"] is False and body["can_release"] is False


def test_a_named_holder_may_hold_but_never_release(settings):
    settings.CANOPY_FLEET_HOLDERS = ["ada@dimagi-ai.com"]
    _u, ws, c, runner = _ctx(superuser=False, email="Ada@dimagi-ai.com")
    _queued(ws)

    body = c.get("/api/harness/fleet-hold").json()
    assert body["can_hold"] is True and body["can_release"] is False

    assert c.post("/api/harness/fleet-hold", data={"note": "leak"},
                  content_type="application/json").status_code == 200
    assert claim.claim_next_turn(runner) is None

    assert c.post("/api/harness/fleet-hold/release").status_code == 403
    assert FleetHold.current().held is True


def test_a_superuser_may_release_a_hold_a_holder_set(settings):
    settings.CANOPY_FLEET_HOLDERS = ["ada@dimagi-ai.com"]
    ada = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    ada_client = Client()
    ada_client.force_login(ada)
    _u, _ws, c, _runner = _ctx()
    ada_client.post("/api/harness/fleet-hold", data={}, content_type="application/json")

    assert c.get("/api/harness/fleet-hold").json()["can_release"] is True
    assert c.post("/api/harness/fleet-hold/release").json()["held"] is False


def test_the_stuck_list_names_the_hold_as_the_reason(settings):
    user, ws, _c, _runner = _ctx()
    turn = _queued(ws)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK,
                                       defaults={"held": True, "note": "tracing"})

    rows = claim.unclaimable_queued_turns(user)

    assert [r["kind"] for r in rows] == ["hold"]
    assert "fleet is on hold (tracing)" in rows[0]["reason"]


# --- stop everything: a separate one-shot action ------------------------------------
#
# Asked for on 2026-10-29: the people who may stop the fleet must be able to stop it
# NOW, not only keep new work from starting. The hold is a toggle; `/fleet-stop` is an
# action beside it that stops every working session and cancels every executing turn.
# Superuser only — it cuts people's work off mid-turn, so a named holder (an agent) may not.


def _working_session(ws, runner, owner, key="hal-turn-1"):
    from apps.agents.models import Agent
    from apps.canopy_sessions.models import RunnerBinding, Session

    agent = Agent.objects.create(slug=f"a-{key}", name="A", workspace=ws, owner=owner)
    session = Session.objects.create(agent=agent, workspace=ws, origin=Session.ORIGIN_RUNNER, title=key)
    RunnerBinding.objects.create(session=session, runner=runner, session_key=key, agent_status="working")
    return session


def _published(monkeypatch):
    from apps.realtime import groups

    sent = []
    monkeypatch.setattr(groups, "publish", lambda group, msg: sent.append(msg))
    return sent


def test_stop_interrupts_working_sessions_and_cancels_executing_turns(monkeypatch):
    user, ws, c, runner = _ctx()
    sent = _published(monkeypatch)
    session = _working_session(ws, runner, user)
    idle = _working_session(ws, runner, user, key="hal-idle")
    idle.runner_binding.agent_status = "awaiting-input"
    idle.runner_binding.save()
    running = _queued(ws)
    Turn.objects.filter(pk=running.pk).update(status=Turn.RUNNING, claimed_by=runner)
    waiting = _queued(ws)

    body = c.post("/api/harness/fleet-stop").json()

    assert body == {"stopped_sessions": 1, "stopped_turns": 1}
    interrupts = [m for m in sent if m["type"] == "runner.session_interrupt"]
    assert [m["session_id"] for m in interrupts] == [str(session.id)]
    assert {"type": "runner.cancel", "turn_id": str(running.id)} in sent
    assert running.events.filter(kind="cancel_requested").exists()
    waiting.refresh_from_db()
    assert waiting.status == Turn.QUEUED
    # A separate action: stopping does not turn the hold on.
    assert FleetHold.current().held is False


def test_a_turn_a_session_stop_already_reached_is_not_cancelled_twice(monkeypatch):
    _user, ws, c, runner = _ctx()
    sent = _published(monkeypatch)
    running = _queued(ws)
    Turn.objects.filter(pk=running.pk).update(status=Turn.RUNNING, claimed_by=runner)
    running.events.create(seq=1, kind="cancel_requested", payload={})

    body = c.post("/api/harness/fleet-stop").json()

    assert body["stopped_turns"] == 0
    assert not [m for m in sent if m["type"] == "runner.cancel"]


def test_holding_stops_nothing_that_is_running(monkeypatch):
    user, ws, c, runner = _ctx()
    sent = _published(monkeypatch)
    _working_session(ws, runner, user)

    assert c.post("/api/harness/fleet-hold", data={}, content_type="application/json").status_code == 200
    assert not [m for m in sent if m["type"] == "runner.session_interrupt"]


def test_a_named_holder_may_hold_but_not_stop_everything(settings, monkeypatch):
    settings.CANOPY_FLEET_HOLDERS = ["ada@dimagi-ai.com"]
    user, ws, c, runner = _ctx(superuser=False, email="ada@dimagi-ai.com")
    sent = _published(monkeypatch)
    _working_session(ws, runner, user)

    assert c.get("/api/harness/fleet-hold").json()["can_stop_running"] is False
    assert c.post("/api/harness/fleet-stop").status_code == 403
    assert not sent
