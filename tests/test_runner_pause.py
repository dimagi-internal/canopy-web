"""Operator pause — stop ROUTING work to a runner without decommissioning it.

Jonathan runs the fleet under two macOS accounts on one laptop for token-limit
failover (Runner.host says so). When one account hits its session limit, the work
has to move to the other one — and that means silencing the limited account's
runner FROM THE OTHER ACCOUNT, which was impossible: the only pause was the local
`~/.canopy/PAUSED` sentinel, and ~/.canopy over there is owned by the other user.
The only reachable lever was `retire`, which is a decommission (it deletes
RunnerAssignment rows `unretire` does not restore, and 404s the daemon's own
heartbeat — it cost jj-mbp-cdp ten sessions on 2026-07-25).

The design rule these tests exist to hold:

  ENFORCEMENT IS SERVER-SIDE, so a pause binds without the runner's cooperation
  and without a deploy on that box. The runner MAY additionally honor it (to stop
  the work it starts by itself), but it is never asked to be the gate.

  ONE WRITER PER STATE. `heartbeat()` must never clear a pause, and pausing must
  never be inferred from what a runner reports. The server flag and the local
  sentinel are two INDEPENDENT stop conditions, not two copies of one state — so
  there is nothing to drift.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    runner = Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, workspace=ws, paired_by=user,
        host="jjackson@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["connect-labs"]},
    )
    return user, ws, c, runner


def _queued_project_turn(ws, project="connect-labs", **kw):
    return Turn.objects.create(
        project=project, workspace=ws, status=Turn.QUEUED,
        origin="api", idempotency_key=f"k-{project}-{Turn.objects.count()}", **kw)


# --- the endpoints ---------------------------------------------------------------

def test_pause_marks_the_runner_and_reports_why():
    _u, _ws, c, runner = _ctx()

    resp = c.post(f"/api/harness/runners/{runner.id}/pause",
                  data={"note": "rate limited until 10:10"},
                  content_type="application/json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["paused_note"] == "rate limited until 10:10"
    assert body["paused_at"] is not None
    runner.refresh_from_db()
    assert runner.paused is True


def test_unpause_clears_it():
    _u, _ws, c, runner = _ctx()
    c.post(f"/api/harness/runners/{runner.id}/pause", data={"note": "x"},
           content_type="application/json")

    resp = c.post(f"/api/harness/runners/{runner.id}/unpause")

    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    assert resp.json()["paused_note"] == ""
    runner.refresh_from_db()
    assert runner.paused is False and runner.paused_at is None


def test_pause_is_idempotent_and_refreshes_the_note():
    """A retry after a dropped response must not error — and must not look like a
    second, different pause."""
    _u, _ws, c, runner = _ctx()
    c.post(f"/api/harness/runners/{runner.id}/pause", data={"note": "first"},
           content_type="application/json")
    first_at = Runner.objects.get(pk=runner.pk).paused_at

    resp = c.post(f"/api/harness/runners/{runner.id}/pause", data={"note": "second"},
                  content_type="application/json")

    assert resp.status_code == 200
    runner.refresh_from_db()
    assert runner.paused_note == "second"
    assert runner.paused_at == first_at, "a re-pause must not restamp when it began"


def test_unpause_is_idempotent_on_a_running_runner():
    _u, _ws, c, runner = _ctx()
    assert c.post(f"/api/harness/runners/{runner.id}/unpause").status_code == 200


def test_pause_does_not_hide_the_runner():
    """The contrast with retire, which makes it invisible. A parked box you cannot
    see is a box nobody remembers to unpause."""
    _u, _ws, c, runner = _ctx()
    c.post(f"/api/harness/runners/{runner.id}/pause", data={},
           content_type="application/json")

    rows = c.get("/api/harness/runners/").json()

    assert [r["name"] for r in rows] == ["jj-mbp-cdp"]
    assert rows[0]["status"] == "paused"


def test_pause_keeps_assignments_unlike_retire():
    """The whole argument for pause existing as its own verb."""
    from apps.harness.models import RunnerAssignment
    _u, ws, c, runner = _ctx()
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    RunnerAssignment.objects.create(runner=runner, agent=agent, rank=0)

    c.post(f"/api/harness/runners/{runner.id}/pause", data={},
           content_type="application/json")
    assert RunnerAssignment.objects.filter(runner=runner).count() == 1

    c.post(f"/api/harness/runners/{runner.id}/retire")
    assert RunnerAssignment.objects.filter(runner=runner).count() == 0


# --- live_status: where every consumer inherits the pause ------------------------

def test_live_status_reads_paused():
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    assert runner.live_status == Runner.PAUSED


def test_is_available_is_false_while_paused():
    """The cascade probe inherits the pause with no edit — the point of deriving
    it in live_status rather than as a flag every caller must remember."""
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    assert runner.is_available is False


def test_staleness_outranks_paused():
    """A parked box whose heartbeat lapsed is ASLEEP, and saying so is more useful
    than saying parked. The pause is still there when it wakes."""
    import datetime as dt
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    runner.last_heartbeat_at = timezone.now() - dt.timedelta(minutes=30)
    assert runner.live_status == Runner.STALE


def test_retired_outranks_paused():
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    runner.status = Runner.RETIRED
    assert runner.live_status == Runner.RETIRED


# --- the enforcement -------------------------------------------------------------

def test_a_paused_runner_claims_nothing():
    _u, ws, _c, runner = _ctx()
    _queued_project_turn(ws)
    assert services.claim_next_turn(runner) is not None, "sanity: claimable when live"

    turn2 = _queued_project_turn(ws)
    runner.paused = True
    runner.save(update_fields=["paused"])

    assert services.claim_next_turn(runner) is None
    turn2.refresh_from_db()
    assert turn2.status == Turn.QUEUED, "the turn waits, it is not consumed or failed"


def test_a_pause_outranks_a_pin():
    """A pin is operator intent, but so is a pause — and it is the more specific
    and more recent one. Letting a pin resurrect a parked box would re-open exactly
    the hole this closes: work landing on an account that must not spend tokens."""
    _u, ws, _c, runner = _ctx()
    turn = _queued_project_turn(ws, pinned_runner=runner)
    runner.paused = True
    runner.save(update_fields=["paused"])

    assert services.claim_next_turn(runner) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED, "it lands on unpause, it is not lost"


def test_unpausing_makes_the_backlog_claimable_again():
    _u, ws, c, runner = _ctx()
    _queued_project_turn(ws)
    runner.paused = True
    runner.save(update_fields=["paused"])
    assert services.claim_next_turn(runner) is None

    c.post(f"/api/harness/runners/{runner.id}/unpause")

    runner.refresh_from_db()
    assert services.claim_next_turn(runner) is not None


def test_another_runner_still_claims_while_this_one_is_paused():
    """The actual user-switch: park one box so the OTHER one gets the work."""
    _u, ws, _c, runner = _ctx()
    other = Runner.objects.create(
        name="acedimagi-mbp-cdp", kind=Runner.EMDASH, workspace=ws,
        paired_by=runner.paired_by, host="acedimagi@mbp", status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={"projects": ["connect-labs"]},
    )
    _queued_project_turn(ws)
    runner.paused = True
    runner.save(update_fields=["paused"])

    assert services.claim_next_turn(runner) is None
    assert services.claim_next_turn(other) is not None


# --- one writer per state --------------------------------------------------------

def test_a_heartbeat_does_not_clear_a_pause():
    """THE regression that would silently reopen the hole: the runner keeps
    heartbeating while parked (that is how we know it is alive), so if heartbeat()
    touched this flag the pause would evaporate within five seconds."""
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    runner.paused_note = "rate limited"
    runner.save(update_fields=["paused", "paused_note"])

    services.heartbeat(runner, active_turn_ids=[], note="", ready=True)

    runner.refresh_from_db()
    assert runner.paused is True
    assert runner.paused_note == "rate limited"
    assert runner.live_status == Runner.PAUSED


def test_a_degraded_heartbeat_does_not_clear_a_pause_either():
    _u, _ws, _c, runner = _ctx()
    runner.paused = True
    runner.save(update_fields=["paused"])

    services.heartbeat(runner, active_turn_ids=[], degraded=True, note="cdp down",
                       ready=False)

    runner.refresh_from_db()
    assert runner.paused is True
