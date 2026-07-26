"""API tests for /api/agents/{slug}/schedules — human CRUD + run-now."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def client():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def agent(default_workspace):
    # Homed to the default workspace, whose auto_join_domains covers the
    # @dimagi.com `client` user above — so the membership gate passes for the
    # legitimate caller and these tests keep exercising CRUD, not tenancy.
    return Agent.objects.create(slug="echo", name="Echo", workspace=default_workspace)


def _create(client, **over):
    body = {
        "name": "Weekly manager report", "prompt": "/echo:manager-report",
        "cron": "0 9 * * 5", "timezone": "America/New_York",
    }
    body.update(over)
    return client.post(
        "/api/agents/echo/schedules/", body, content_type="application/json"
    )


def test_create_and_list(client, agent):
    resp = _create(client)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["agent_slug"] == "echo"
    assert body["cron"] == "0 9 * * 5"
    assert len(body["next_runs"]) == 3  # the UI preview

    listing = client.get("/api/agents/echo/schedules/")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_list_limit_zero_does_not_500(client, agent):
    """?limit=0 does NOT 422 — list_schedules clamps via
    max(1, min(limit, 500)) before the value ever reaches Page (whose `limit`
    is Field(ge=1, le=500)), so an out-of-range limit is floored to a valid
    one instead of crashing inside the response model. Pins the clamp: 200,
    with the clamped limit reflected in the page and exactly one item
    returned (the one schedule created below).
    """
    _create(client)

    resp = client.get("/api/agents/echo/schedules/?limit=0")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["limit"] == 1  # max(1, min(0, 500)) == 1
    assert len(body["items"]) == 1
    assert body["total"] == 1


def test_bad_cron_is_422_problem_json(client, agent):
    resp = _create(client, cron="every friday please")
    assert resp.status_code == 422
    assert resp["content-type"] == "application/problem+json"


def test_patch_toggles_enabled(client, agent):
    sid = _create(client).json()["id"]
    resp = client.patch(
        f"/api/agents/echo/schedules/{sid}", {"enabled": False},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_patch_with_explicit_null_does_not_500(client, agent):
    """An explicit {"cron": null} must not setattr None onto a non-nullable
    column. SchedulePatch's validator short-circuits on None, so this can only
    be caught here."""
    sid = _create(client).json()["id"]
    resp = client.patch(
        f"/api/agents/echo/schedules/{sid}", {"cron": None},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["cron"] == "0 9 * * 5"  # unchanged, not nulled


def test_delete(client, agent):
    sid = _create(client).json()["id"]
    assert client.delete(f"/api/agents/echo/schedules/{sid}").status_code == 204
    assert AgentSchedule.objects.count() == 0


def test_delete_supersedes_open_occurrences_so_the_agent_is_not_wedged(client, agent):
    """Deleting a schedule must terminate its open occurrences FIRST.

    An executing turn holds `one_executing_turn_per_agent`, and the runner's
    heartbeat keeps renewing its lease, so `sweep_expired_leases` never rescues
    it — `release_stale_occurrence_turns_all` is the only unwedger, and it
    resolves schedules by id. Delete the row and that turn becomes permanently
    unreleasable: every subsequent turn for the agent is unclaimable forever,
    and the nag that would surface it is gone too.
    """
    from apps.harness import services

    schedule = AgentSchedule.objects.get(pk=_create(client).json()["id"])
    turn, _ = services.fire_schedule(schedule, dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC))
    # Drive it to the state a runner's claim leaves it in — QUEUED holds nothing.
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)

    assert client.delete(f"/api/agents/echo/schedules/{schedule.id}").status_code == 204

    turn.refresh_from_db()
    assert turn.status == Turn.MISSED
    assert turn.finished_at is not None
    # The real proof: the executing-turn index now admits a new turn for the
    # agent. If the old turn were still executing, this insert would raise.
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_MANUAL, idempotency_key="probe",
        status=Turn.RUNNING,
    )


def test_delete_retires_queued_occurrences_too(client, agent):
    """A queued occurrence outlives its schedule otherwise — and would later
    execute a prompt for a schedule that no longer exists."""
    from apps.harness import services

    schedule = AgentSchedule.objects.get(pk=_create(client).json()["id"])
    turn, _ = services.fire_schedule(schedule, dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC))

    client.delete(f"/api/agents/echo/schedules/{schedule.id}")

    turn.refresh_from_db()
    assert turn.status == Turn.MISSED


def test_duplicate_name_is_409_not_500(client, agent):
    """`uniq_agent_schedule_name` is a DB constraint — an uncaught IntegrityError
    surfaces the UI's "New schedule" button as a 500."""
    assert _create(client).status_code == 201

    resp = _create(client)

    assert resp.status_code == 409, resp.content
    assert resp["content-type"] == "application/problem+json"
    assert "Weekly manager report" in resp.json()["detail"]


def test_patch_onto_a_sibling_name_is_409_not_500(client, agent):
    """Same constraint, reached from Edit by renaming onto a sibling."""
    _create(client)
    sid = _create(client, name="Daily standup", cron="0 9 * * 1-5").json()["id"]

    resp = client.patch(
        f"/api/agents/echo/schedules/{sid}", {"name": "Weekly manager report"},
        content_type="application/json",
    )

    assert resp.status_code == 409, resp.content
    assert resp["content-type"] == "application/problem+json"
    assert "Weekly manager report" in resp.json()["detail"]


def test_run_now_enqueues_a_manual_turn(client, agent):
    sid = _create(client).json()["id"]
    resp = client.post(f"/api/agents/echo/schedules/{sid}/run-now")
    assert resp.status_code == 202, resp.content
    turn = Turn.objects.get()
    assert turn.origin == Turn.ORIGIN_MANUAL
    assert turn.prompt == "/echo:manager-report"


def test_unknown_agent_404s(client):
    assert client.get("/api/agents/nope/schedules/").status_code == 404


def test_fire_after_defaults_to_created_at_not_null(client, agent):
    """A fresh schedule must never fire for a slot that predates it.

    last_slot is NULL until the first fire, and due_slot(after=None) looks
    backward with no lower bound — so a schedule created Wednesday would
    immediately owe LAST Friday's report. fire_after is the server-computed
    anchor that closes that hole; the runner passes it straight to due_slot.
    """
    body = _create(client).json()
    schedule = AgentSchedule.objects.get(pk=body["id"])

    assert body["last_slot"] is None
    assert body["fire_after"] == schedule.created_at.isoformat().replace("+00:00", "Z")


def test_fire_after_tracks_last_slot_once_fired(client, agent):
    from apps.harness import services

    schedule = AgentSchedule.objects.get(pk=_create(client).json()["id"])
    slot = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC)
    services.fire_schedule(schedule, slot)

    body = client.get("/api/agents/echo/schedules/").json()["items"][0]

    assert body["fire_after"] == slot.isoformat().replace("+00:00", "Z")


def test_preview_returns_three_fire_times(client, agent):
    resp = client.post(
        "/api/agents/echo/schedules/preview",
        {"cron": "0 9 * * 5", "timezone": "America/New_York"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert len(resp.json()["next_runs"]) == 3


def test_preview_rejects_a_bad_cron_without_saving_anything(client, agent):
    resp = client.post(
        "/api/agents/echo/schedules/preview",
        {"cron": "every friday please", "timezone": "UTC"},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert resp["content-type"] == "application/problem+json"
    assert AgentSchedule.objects.count() == 0


# --- tenancy: _agent_or_404's membership branch --------------------------
#
# The fixtures above home their agent in the DEFAULT workspace, whose
# auto_join_domains admits any @dimagi.com caller — fine for exercising CRUD,
# useless for exercising the gate. These tests give the agent a REAL workspace
# with `auto_join_domains=[]` (load-bearing: `_agent_or_404` calls
# `wsvc.auto_join_workspaces(request.user)` first, so a nonempty
# auto_join_domains matching the outsider's email domain would silently make
# them a member and the "non-member" test would pass while testing nothing)
# so the membership check is the only thing standing between the caller and
# the data.


@pytest.fixture()
def scoped_workspace():
    owner = User.objects.create_user("acme-owner", "acme-owner@dimagi.com", "pw")
    return Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=owner, auto_join_domains=[]
    )


@pytest.fixture()
def scoped_agent(scoped_workspace):
    return Agent.objects.create(slug="acme-echo", name="Acme Echo", workspace=scoped_workspace)


def _schedule_body(**over):
    body = {
        "name": "Weekly manager report", "prompt": "/echo:manager-report",
        "cron": "0 9 * * 5", "timezone": "America/New_York",
    }
    body.update(over)
    return body


def test_non_member_gets_404_not_403_on_every_route_and_nothing_leaks_or_writes(
    scoped_workspace, scoped_agent
):
    outsider = User.objects.create_user("outsider", "outsider@dimagi.com", "pw")
    # Guard: prove auto-join didn't silently make the outsider a member —
    # otherwise this whole test would pass for the wrong reason.
    assert not wsvc.is_member(outsider, scoped_workspace.slug)

    c = Client()
    c.force_login(outsider)
    base = f"/api/agents/{scoped_agent.slug}/schedules/"

    list_resp = c.get(base)
    create_resp = c.post(base, _schedule_body(), content_type="application/json")
    preview_resp = c.post(
        base + "preview", {"cron": "0 9 * * 5", "timezone": "UTC"},
        content_type="application/json",
    )
    patch_resp = c.patch(base + "999", {"enabled": False}, content_type="application/json")
    delete_resp = c.delete(base + "999")
    run_now_resp = c.post(base + "999/run-now")

    for label, resp in [
        ("list", list_resp), ("create", create_resp), ("preview", preview_resp),
        ("patch", patch_resp), ("delete", delete_resp), ("run-now", run_now_resp),
    ]:
        assert resp.status_code == 404, f"{label}: expected 404, got {resp.status_code} ({resp.content})"
        assert resp.status_code != 403, f"{label}: leaked existence via 403 instead of 404"

    # No data leaked out through list, and the create attempt did not land.
    assert AgentSchedule.objects.filter(agent=scoped_agent).count() == 0


def test_member_of_the_agents_workspace_can_reach_every_route(scoped_workspace, scoped_agent):
    """Sanity check for the test above: if the gate rejected EVERYONE (not just
    non-members), the 404 assertions would pass for the wrong reason too."""
    member = User.objects.create_user("member", "member@dimagi.com", "pw")
    wsvc.ensure_member(scoped_workspace, member, WorkspaceMembership.OWNER)
    assert wsvc.is_member(member, scoped_workspace.slug)

    c = Client()
    c.force_login(member)
    base = f"/api/agents/{scoped_agent.slug}/schedules/"

    create_resp = c.post(base, _schedule_body(), content_type="application/json")
    assert create_resp.status_code == 201, create_resp.content
    sid = create_resp.json()["id"]

    list_resp = c.get(base)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    preview_resp = c.post(
        base + "preview", {"cron": "0 9 * * 5", "timezone": "UTC"},
        content_type="application/json",
    )
    assert preview_resp.status_code == 200

    patch_resp = c.patch(base + f"{sid}", {"enabled": False}, content_type="application/json")
    assert patch_resp.status_code == 200

    run_now_resp = c.post(base + f"{sid}/run-now")
    assert run_now_resp.status_code == 202

    delete_resp = c.delete(base + f"{sid}")
    assert delete_resp.status_code == 204
    assert AgentSchedule.objects.filter(agent=scoped_agent).count() == 0
