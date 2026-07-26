"""Request-free schedule service layer — auth resolution + CRUD, shared by the
REST routes and the MCP tools."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness import schedule_services as ss
from apps.harness import services
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", created_by=owner, auto_join_domains=[]
    )
    wsvc.ensure_member(w, owner, WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def agent(ws):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws)


def test_resolve_agent_for_member(owner, agent):
    assert ss._resolve_agent(owner, "eva").slug == "eva"


def test_resolve_agent_missing_raises_not_found(owner, ws):
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(owner, "nope")


def test_resolve_agent_non_member_raises_not_found(agent):
    """A non-member gets ScheduleNotFound — the same as a missing agent, so
    tenancy never leaks existence."""
    outsider = User.objects.create_user("mallory", "mallory@evil.com", "pw")
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(outsider, "eva")


def test_resolve_agent_wrong_tenant_pin_raises_not_found(owner, agent):
    """The workspace_slug pin (the REST tenant-URL) must match the agent's."""
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(owner, "eva", workspace_slug="some-other-ws")


def test_resolve_schedule_wrong_agent_raises_not_found(owner, agent, ws):
    other = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    sched = AgentSchedule.objects.create(
        agent=other, name="s", prompt="p", cron="0 9 * * 5", timezone="UTC"
    )
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_schedule(owner, "eva", sched.id)


def _fields(**over):
    f = dict(
        name="Goal review", prompt="/eva:goal-review", cron="0 9 1 * *",
        timezone="America/New_York", enabled=True, routing="prefer_local",
        grace_minutes=120, notify=["inbox"],
    )
    f.update(over)
    return f


def test_create_and_list(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    assert s.name == "Goal review"
    assert [x.id for x in ss.list_schedules(owner, "eva")] == [s.id]


def test_create_duplicate_name_raises(owner, agent):
    ss.create_schedule(owner, "eva", _fields())
    with pytest.raises(ss.DuplicateScheduleName) as exc:
        ss.create_schedule(owner, "eva", _fields(prompt="different"))
    assert exc.value.name == "Goal review"


def test_update_applies_only_supplied_fields(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    out = ss.update_schedule(owner, "eva", s.id, {"enabled": False})
    assert out.enabled is False
    assert out.cron == "0 9 1 * *"  # untouched


def test_serialize_shape(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    d = ss.serialize_schedule(s)
    assert d["agent_slug"] == "eva"
    assert d["fire_after"] == s.created_at  # last_slot is None -> created_at
    assert len(d["next_runs"]) == 3
    assert d["last_status"] == ""


def test_delete_supersedes_open_turns_then_removes(owner, agent):
    """The wedge regression: an executing occurrence must be retired BEFORE the
    row is deleted, or it holds one_executing_turn_per_agent forever."""
    s = ss.create_schedule(owner, "eva", _fields())
    turn, _ = services.fire_schedule(s, dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC))
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)

    ss.delete_schedule(owner, "eva", s.id)

    turn.refresh_from_db()
    assert turn.status == Turn.MISSED  # retired, not stranded
    assert not AgentSchedule.objects.filter(pk=s.id).exists()
    # Proof it is unwedged: a new executing turn for the agent is insertable.
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="b1", status=Turn.RUNNING
    )


def test_run_now_enqueues_manual_turn(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    ss.run_schedule_now(owner, "eva", s.id)
    assert Turn.objects.filter(origin=Turn.ORIGIN_MANUAL).count() == 1


def test_preview_cron_returns_three(owner, agent):
    out = ss.preview_cron(owner, "eva", "0 9 * * 5", "America/New_York")
    assert len(out) == 3


def test_mcp_shape_no_workspace_pin_still_gated(agent):
    """workspace_slug=None (the MCP path) still requires membership."""
    outsider = User.objects.create_user("m", "m@evil.com", "pw")
    with pytest.raises(ss.ScheduleNotFound):
        ss.list_schedules(outsider, "eva")


def test_week_schedules_gathers_enabled_with_fires(owner, agent, ws):
    ss.create_schedule(owner, "eva", _fields(name="Daily", cron="0 9 * * *", timezone="UTC"))
    ss.create_schedule(owner, "eva", _fields(name="Paused", cron="0 9 * * *", timezone="UTC", enabled=False))
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)

    rows = ss.week_schedules({ws.slug}, start)

    assert len(rows) == 1  # the disabled one is excluded
    row = rows[0]
    assert row["schedule"]["name"] == "Daily"
    assert row["workspace_slug"] == ws.slug
    assert len(row["fires"]) == 8  # daily; window over-fetches 8 days (client trims to 7)


def test_week_schedules_includes_final_local_hour_on_fallback_week(owner, agent, ws):
    """DST fall-back regression: the grid renders 7 LOCAL calendar days, which is
    169h on a fall-back week (a 25h local day), but `start` is a fixed UTC instant.
    A schedule firing in the final local hour of Sunday lands in the Sunday column
    yet past a fixed 7*24h=168h window — dropped from the response. week_schedules
    over-fetches (8 days) so no local-day fire is ever missed.

    America/New_York, week of Mon 2026-10-26 (fall-back Sun 2026-11-01), daily
    23:30 local: all 7 fires must be returned, not 6 (the last one, Sun 23:30,
    is what a 168h window drops)."""
    ss.create_schedule(
        owner, "eva",
        _fields(name="Late daily", cron="30 23 * * *", timezone="America/New_York"),
    )
    # Local-Monday-midnight as the fixed UTC instant the client sends: in late
    # October NY is EDT (UTC-4), so Mon 2026-10-26 00:00 local = 04:00 UTC.
    start = dt.datetime(2026, 10, 26, 4, 0, tzinfo=dt.UTC)

    rows = ss.week_schedules({ws.slug}, start)

    row = next(r for r in rows if r["schedule"]["name"] == "Late daily")
    assert len(row["fires"]) == 7  # not 6 — the fall-back-Sunday fire is included


def test_week_schedules_scoped_to_given_workspaces(owner, agent, ws):
    # A schedule in a workspace NOT in the set must not appear.
    ss.create_schedule(owner, "eva", _fields(cron="0 9 * * *"))
    rows = ss.week_schedules({"some-other-ws"}, dt.datetime(2026, 7, 13, tzinfo=dt.UTC))
    assert rows == []


def test_week_schedules_never_matches_on_a_null_workspace(agent):
    """`workspace_ids` used to be allowed to contain `None`, meaning "also match
    legacy unhomed agents", which week_schedules turned into an OR'd
    `Q(agent__workspace_id__isnull=True)` — leaking every unhomed agent's
    schedule name, prompt, cron and timezone to any authenticated flat-route
    caller (`_visible_workspace_ids` unioned `{None}` in on the unpinned
    branch). Both the leg and the row it admitted are gone (agents/0013).

    A stray `None` in the set must therefore match NOTHING rather than
    everything — Django's `__in` silently drops it, which is now exactly the
    desired behaviour rather than the bug the isnull branch worked around."""
    AgentSchedule.objects.create(
        agent=agent, name="Homed sched", prompt="p", cron="0 9 * * *", timezone="UTC"
    )
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)

    assert ss.week_schedules({None}, start) == []
    assert ss.week_schedules(set(), start) == []
    rows = ss.week_schedules({agent.workspace_id}, start)
    assert [r["schedule"]["name"] for r in rows] == ["Homed sched"]
