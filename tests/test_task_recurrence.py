"""Recurring tasks: a template task with a rhythm, and the instances canopy makes of it.

A task with `recurrence` is a TEMPLATE: it stays in backlog and never completes. An
INSTANCE is an ordinary task whose `template` names it, made only by
`services.recurrence.spawn` (idempotent per template and period). The tick spawns `auto`
templates at the start of each period, in the workspace's own timezone; `as_needed` is
spawned by hand only."""
from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents import work
from apps.agents.models import Agent, AgentProject, AgentTask, TaskEvent
from apps.agents.services import recurrence
from apps.common import ticks
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
J = "application/json"
UTC = dt.UTC


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="home", display_name="Home", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    addy = User.objects.create_user("addy", "addy@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=addy, workspace=ws, role=WorkspaceMembership.VIEWER)
    bailey = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=owner)
    project = AgentProject.objects.create(workspace=ws, handle="chores", name="Chores", owner_user=owner)
    c = Client()
    c.force_login(owner)
    ac = Client()
    ac.force_login(addy)
    return {"owner": owner, "ws": ws, "addy": addy, "bailey": bailey, "project": project, "c": c, "ac": ac}


def _template(c, every="week", **extra):
    rec = {"every": every, **{k: extra.pop(k) for k in ("weekday", "auto") if k in extra}}
    body = {"title": "Take out the trash", "workspace": "home", "project": "home/chores", "recurrence": rec,
            "notes": "Bins at the curb", **extra}
    r = c.post("/api/tasks/", body, content_type=J)
    assert r.status_code == 201, r.content
    return r.json()


def _created_on(key: str, when: dt.datetime) -> AgentTask:
    AgentTask.objects.filter(key=key).update(created_at=when)
    return AgentTask.objects.get(key=key)


# ---- validation -------------------------------------------------------------


@pytest.mark.parametrize("rec", [
    {"every": "day"}, {"every": "week", "weekday": "thu", "auto": True}, {"every": "2weeks", "auto": True},
    {"every": "month", "auto": True}, {"every": "as_needed"},
])
def test_a_valid_recurrence_is_kept_normalised(rec):
    out = recurrence.clean(rec)
    assert out["every"] == rec["every"] and out["auto"] == rec.get("auto", False)
    assert out.get("weekday") == rec.get("weekday")


@pytest.mark.parametrize("rec, why", [
    ({"every": "fortnight"}, "every"),
    ({"every": "week", "weekday": "thursday"}, "weekday"),
    ({"every": "day", "weekday": "mon"}, "weekday"),
    ({"every": "month", "weekday": "mon"}, "weekday"),
    ({"every": "as_needed", "auto": True}, "as_needed"),
    ({"every": "week", "auto": "yes"}, "auto"),
    ({"every": "week", "extra": 1}, "extra"),
    ("week", "object"),
])
def test_a_bad_recurrence_is_refused(rec, why):
    with pytest.raises(ValueError, match=why):
        recurrence.clean(rec)


def test_null_means_not_recurring():
    assert recurrence.clean(None) is None


def test_a_template_is_made_in_backlog_whatever_status_is_sent(w):
    t = _template(w["c"], status="todo")
    assert t["status"] == "backlog" and t["recurrence"] == {"every": "week", "weekday": None, "auto": False}
    assert t["template_key"] is None and t["last_done"] is None


def test_a_bad_recurrence_on_create_is_422(w):
    r = w["c"].post("/api/tasks/", {"title": "x", "workspace": "home", "recurrence": {"every": "hourly"}},
                    content_type=J)
    assert r.status_code == 422


def test_a_template_never_completes(w):
    t = _template(w["c"])
    for status in ("done", "todo", "declined", "in_progress"):
        r = w["c"].patch(f"/api/tasks/{t['key']}/", {"status": status}, content_type=J)
        assert r.status_code == 422, (status, r.content)
    for action in ("done", "approve", "decline", "accept", "nudge"):
        r = w["c"].post(f"/api/tasks/{t['key']}/actions", {"action": action}, content_type=J)
        assert r.status_code in (409, 422), (action, r.content)
    assert AgentTask.objects.get(key=t["key"]).status == AgentTask.BACKLOG


def test_patching_a_recurrence_on_makes_it_a_template_and_null_makes_it_a_task_again(w):
    r = w["c"].post("/api/tasks/", {"title": "Water plants", "workspace": "home", "status": "todo"}, content_type=J)
    key = r.json()["key"]
    on = w["c"].patch(f"/api/tasks/{key}/", {"recurrence": {"every": "day", "auto": True}}, content_type=J)
    assert on.status_code == 200 and on.json()["status"] == "backlog"
    assert on.json()["recurrence"] == {"every": "day", "weekday": None, "auto": True}
    off = w["c"].patch(f"/api/tasks/{key}/", {"recurrence": None}, content_type=J)
    assert off.status_code == 200 and off.json()["recurrence"] is None
    assert w["c"].patch(f"/api/tasks/{key}/", {"status": "done"}, content_type=J).status_code == 200
    fields = set(TaskEvent.objects.filter(task__key=key, kind=TaskEvent.FIELD).values_list("field", flat=True))
    assert "recurrence" in fields


def test_an_instance_cannot_become_a_template(w):
    t = _template(w["c"])
    inst, _ = recurrence.spawn(AgentTask.objects.get(key=t["key"]), dt.date(2026, 11, 12), by=w["owner"])
    r = w["c"].patch(f"/api/tasks/{inst.key}/", {"recurrence": {"every": "week"}}, content_type=J)
    assert r.status_code == 422


# ---- spawn ------------------------------------------------------------------


def test_spawn_copies_the_template_and_is_idempotent_per_period(w):
    t = _template(w["c"], every="week", weekday="thu", assignee="addy@dimagi.com", labels=["outside"])
    template = AgentTask.objects.get(key=t["key"])
    first, made = recurrence.spawn(template, dt.date(2026, 11, 12), by=w["owner"])  # a Thursday
    assert made
    assert (first.title, first.notes, first.assignee_user, first.project_id, first.template_id) == (
        "Take out the trash", "Bins at the curb", w["addy"], w["project"].pk, template.pk)
    assert first.due == dt.date(2026, 11, 12) and first.status == AgentTask.TODO
    assert [lb.name for lb in first.labels.all()] == ["outside"]
    assert first.events.filter(kind=TaskEvent.CREATED, by_user=w["owner"]).exists()

    again, made = recurrence.spawn(template, dt.date(2026, 11, 10), by=w["owner"])  # same week, another day
    assert not made and again.pk == first.pk
    nxt, made = recurrence.spawn(template, dt.date(2026, 11, 19), by=w["owner"])
    assert made and nxt.pk != first.pk
    # The period is stamped when it is made: moving an instance's due does not free its period.
    AgentTask.objects.filter(pk=first.pk).update(due=dt.date(2026, 11, 30))
    assert recurrence.spawn(template, dt.date(2026, 11, 13), by=w["owner"])[0].pk == first.pk


def test_spawn_copies_an_agent_assignee_and_starts_nothing(w):
    t = _template(w["c"], every="day", assignee="agent:bailey")
    inst, _ = recurrence.spawn(AgentTask.objects.get(key=t["key"]), dt.date(2026, 11, 12), by=None)
    assert inst.agent == w["bailey"] and inst.status == AgentTask.TODO
    assert not Turn.objects.exists()


def test_as_needed_is_one_instance_per_date(w):
    template = AgentTask.objects.get(key=_template(w["c"], every="as_needed")["key"])
    a, _ = recurrence.spawn(template, dt.date(2026, 11, 12), by=w["owner"])
    b, made = recurrence.spawn(template, dt.date(2026, 11, 12), by=w["owner"])
    c, made_c = recurrence.spawn(template, dt.date(2026, 11, 13), by=w["owner"])
    assert a.pk == b.pk and not made and made_c and c.pk != a.pk


def test_spawn_refuses_a_task_that_is_not_a_template(w):
    plain = AgentTask.objects.create(workspace=w["ws"], title="once")
    with pytest.raises(ValueError, match="recurring"):
        recurrence.spawn(plain, dt.date(2026, 11, 12), by=w["owner"])


def test_the_spawn_route(w):
    t = _template(w["c"], every="week")
    r = w["c"].post(f"/api/tasks/{t['key']}/spawn", {"due": "2026-11-12"}, content_type=J)
    assert r.status_code == 201, r.content
    inst = r.json()
    assert inst["template_key"] == t["key"] and inst["due"] == "2026-11-12"
    again = w["c"].post(f"/api/tasks/{t['key']}/spawn", {"due": "2026-11-13"}, content_type=J)
    assert again.status_code == 200 and again.json()["key"] == inst["key"]
    # A viewer may not make work; a plain task is not a template.
    assert w["ac"].post(f"/api/tasks/{t['key']}/spawn", {"due": "2026-11-20"}, content_type=J).status_code == 403
    plain = w["c"].post("/api/tasks/", {"title": "once", "workspace": "home"}, content_type=J).json()
    assert w["c"].post(f"/api/tasks/{plain['key']}/spawn", {"due": "2026-11-20"}, content_type=J).status_code == 422


def test_list_filters_recurring_and_template(w):
    t = _template(w["c"])
    inst = w["c"].post(f"/api/tasks/{t['key']}/spawn", {"due": "2026-11-12"}, content_type=J).json()
    plain = w["c"].post("/api/tasks/", {"title": "once", "workspace": "home"}, content_type=J).json()
    keys = lambda q: {x["key"] for x in w["c"].get("/api/tasks/", q).json()}  # noqa: E731
    assert keys({"recurring": "true"}) == {t["key"]}
    assert keys({"recurring": "false"}) == {inst["key"], plain["key"]}
    assert keys({"template": t["key"]}) == {inst["key"]}
    assert keys({"template": t["key"].lower()}) == {inst["key"]}
    assert w["c"].get("/api/tasks/count/", {"recurring": "true"}).json()["count"] == 1


# ---- last_done ---------------------------------------------------------------


def test_last_done_is_the_newest_done_instance(w):
    t = _template(w["c"], every="day")
    template = AgentTask.objects.get(key=t["key"])
    a, _ = recurrence.spawn(template, dt.date(2026, 11, 10), by=w["owner"])
    b, _ = recurrence.spawn(template, dt.date(2026, 11, 11), by=w["owner"])
    recurrence.spawn(template, dt.date(2026, 11, 12), by=w["owner"])
    assert w["c"].get(f"/api/tasks/{t['key']}/").json()["last_done"] is None

    for inst in (a, b):
        assert w["c"].post(f"/api/tasks/{inst.key}/actions", {"action": "done"}, content_type=J).status_code == 200
    done_at = {k: TaskEvent.objects.get(task__key=k, kind=TaskEvent.FIELD, field="status", new="done").created_at
               for k in (a.key, b.key)}
    early, late = sorted(done_at.values())
    TaskEvent.objects.filter(task__key=a.key, field="status", new="done").update(created_at=late + dt.timedelta(hours=1))
    expected = late + dt.timedelta(hours=1)
    detail = w["c"].get(f"/api/tasks/{t['key']}/").json()
    assert dt.datetime.fromisoformat(detail["last_done"]) == expected
    listed = {x["key"]: x for x in w["c"].get("/api/tasks/", {"recurring": "true"}).json()}
    assert dt.datetime.fromisoformat(listed[t["key"]]["last_done"]) == expected
    # Reopened, it no longer counts.
    w["c"].patch(f"/api/tasks/{a.key}/", {"status": "todo"}, content_type=J)
    assert dt.datetime.fromisoformat(w["c"].get(f"/api/tasks/{t['key']}/").json()["last_done"]) == done_at[b.key]
    assert early  # both were recorded


# ---- the tick ------------------------------------------------------------------


def _at(y, m, d, h=9, tz=UTC) -> dt.datetime:
    return dt.datetime(y, m, d, h, tzinfo=tz)


def _instances(key: str) -> list[dt.date]:
    return sorted(AgentTask.objects.filter(template__key=key).values_list("due", flat=True))


def test_the_tick_is_registered():
    assert "agents.recurrence" in ticks.jobs()


def test_daily_spawns_once_a_day(w):
    t = _template(w["c"], every="day", auto=True)
    _created_on(t["key"], _at(2026, 11, 9, 8))
    for now in (_at(2026, 11, 9, 10), _at(2026, 11, 9, 23), _at(2026, 11, 10, 0, )):
        recurrence.spawn_due(now)
    assert _instances(t["key"]) == [dt.date(2026, 11, 9), dt.date(2026, 11, 10)]


def test_weekly_spawns_on_monday_due_on_its_weekday_and_not_mid_week_after_creation(w):
    t = _template(w["c"], every="week", weekday="thu", auto=True)
    _created_on(t["key"], _at(2026, 11, 11))  # a Wednesday: this week had begun
    recurrence.spawn_due(_at(2026, 11, 12))
    assert _instances(t["key"]) == []
    recurrence.spawn_due(_at(2026, 11, 16, 0))  # Monday
    recurrence.spawn_due(_at(2026, 11, 18))
    assert _instances(t["key"]) == [dt.date(2026, 11, 19)]


def test_two_weeks_alternates_from_the_creation_week(w):
    t = _template(w["c"], every="2weeks", auto=True)
    _created_on(t["key"], _at(2026, 11, 2, 0, ))  # Monday
    for day in range(2, 31):
        recurrence.spawn_due(_at(2026, 11, day))
    assert _instances(t["key"]) == [dt.date(2026, 11, 2), dt.date(2026, 11, 16), dt.date(2026, 11, 30)]


def test_two_weeks_alternates_from_its_first_instance(w):
    t = _template(w["c"], every="2weeks", auto=True)
    _created_on(t["key"], _at(2026, 11, 4))  # mid-week: no auto instance this week
    template = AgentTask.objects.get(key=t["key"])
    recurrence.spawn(template, dt.date(2026, 11, 10), by=w["owner"])  # by hand, the week after
    for day in range(9, 31):
        recurrence.spawn_due(_at(2026, 11, day))
    assert _instances(t["key"]) == [dt.date(2026, 11, 10), dt.date(2026, 11, 23)]


def test_monthly_spawns_on_the_first(w):
    t = _template(w["c"], every="month", auto=True)
    _created_on(t["key"], _at(2026, 10, 31))
    for now in (_at(2026, 10, 31, 12), _at(2026, 11, 1, 1), _at(2026, 11, 20), _at(2026, 12, 1, 1)):
        recurrence.spawn_due(now)
    assert _instances(t["key"]) == [dt.date(2026, 11, 1), dt.date(2026, 12, 1)]


def test_the_period_turns_at_local_midnight_in_the_workspace_timezone(w):
    work.set_settings("home", {"timezone": "America/New_York"})
    ny = zoneinfo.ZoneInfo("America/New_York")
    t = _template(w["c"], every="day", auto=True)
    _created_on(t["key"], dt.datetime(2026, 11, 9, 12, tzinfo=ny))
    # 03:00 UTC on the 10th is still the 9th in New York.
    recurrence.spawn_due(dt.datetime(2026, 11, 10, 3, tzinfo=UTC))
    assert _instances(t["key"]) == [dt.date(2026, 11, 9)]
    recurrence.spawn_due(dt.datetime(2026, 11, 10, 5, 30, tzinfo=UTC))  # 00:30 on the 10th in New York
    assert _instances(t["key"]) == [dt.date(2026, 11, 9), dt.date(2026, 11, 10)]


def test_as_needed_and_manual_templates_never_auto_spawn(w):
    for every in ("as_needed", "week"):
        t = _template(w["c"], every=every)  # auto defaults to False
        _created_on(t["key"], _at(2026, 11, 1))
    for day in range(1, 30):
        recurrence.spawn_due(_at(2026, 11, day))
    assert not AgentTask.objects.filter(template__isnull=False).exists()


def test_the_tick_skips_a_template_whose_recurrence_was_cleared(w):
    t = _template(w["c"], every="day", auto=True)
    _created_on(t["key"], _at(2026, 11, 1))
    w["c"].patch(f"/api/tasks/{t['key']}/", {"recurrence": None}, content_type=J)
    recurrence.spawn_due(_at(2026, 11, 2))
    assert _instances(t["key"]) == []


def test_timezone_is_validated(w):
    with pytest.raises(ValueError, match="timezone"):
        work.set_settings("home", {"timezone": "Mars/Olympus"})
    r = w["c"].patch("/api/task-settings/?workspace=home", {"timezone": "Europe/London"}, content_type=J)
    assert r.status_code == 200 and r.json()["timezone"] == "Europe/London"
    assert w["c"].patch("/api/task-settings/?workspace=home", {"timezone": "Nowhere"},
                        content_type=J).status_code == 422


def test_the_tick_never_spawns_into_a_personal_project_for_an_outsider(w):
    """A personal project's template spawns there, assigned as the template is; its
    instance is as private as the template (the walk in test_personal_project_never_leaks
    covers the reads)."""
    project = AgentProject.objects.create(workspace=w["ws"], handle="mine", name="Mine", owner_user=w["owner"],
                                          visibility=AgentProject.PERSONAL)
    t = _template(w["c"], every="day", auto=True, project="home/mine")
    _created_on(t["key"], timezone.now() - dt.timedelta(days=1))
    recurrence.spawn_due()
    inst = AgentTask.objects.get(template__key=t["key"])
    assert inst.project == project
    assert w["ac"].get(f"/api/tasks/{inst.key}/").status_code == 404
    assert inst.key not in w["ac"].get("/api/tasks/", {"template": t["key"]}).content.decode()
    assert w["ac"].post(f"/api/tasks/{t['key']}/spawn", {"due": "2026-11-20"}, content_type=J).status_code == 404
