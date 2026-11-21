"""Where a learner runs (`BrainTarget`) and how: each target's own schedule on canopy's scheduler.

dimagi → {connect, labs}. A learner with no target never runs. A workspace's owners and admins (or an ancestor's) aim a
learner at their workspace, one below it or a workspace project there, never above or beside. Each target owns a normal
`AgentSchedule` on the operator agent, listed with the agent's other schedules; canopy's clock (or a runner) fires it,
and the fire starts one brain run for that target when there is something new. The brain's own scheduler is gone."""
from __future__ import annotations

import datetime as dt
import importlib

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.brain import associate, reading, registry, sources, targets
from apps.brain.models import BrainCapability, BrainRun, BrainTarget, LearningConsent
from apps.canopy_sessions.models import Session
from apps.events.models import SettingChange
from apps.harness import schedule_clock, schedule_hooks, schedule_turns
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership as M
from tests.fixtures.brain_world import aim, fire, member, reads, user

pytestmark = pytest.mark.django_db


def _client(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w():
    registry.ensure_builtin()
    jj, ca, ce, la, alice, bob = (user(n) for n in ("jj", "connect_admin", "connect_editor", "labs_admin", "alice",
                                                     "bob"))
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=jj, parent=dimagi)
    member(jj, dimagi, M.OWNER)
    member(ca, connect, M.ADMIN)
    member(ce, connect)
    member(la, labs, M.ADMIN)
    for u in (alice, bob):
        member(u, connect)
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    reads(dimagi)
    ada = Agent.objects.create(slug="ada", name="Ada", owner=jj, workspace=dimagi)
    registry.ensure_learners()                                   # system-learning preset: ada, /canopy:brain-learn
    system = BrainCapability.objects.get(slug="system-learning")
    registry.update(system, by=jj, enabled=True)
    s = Session.objects.create(workspace=connect, created_by=alice, origin=Session.ORIGIN_WEB, title="connect work")
    sources.for_session(s)
    return {"jj": jj, "ca": ca, "ce": ce, "la": la, "alice": alice, "dimagi": dimagi, "connect": connect,
            "labs": labs, "ada": ada, "system": BrainCapability.objects.get(pk=system.pk), "session": s}


def _add(u, ws, **body):
    return _client(u).post(f"/api/w/{ws}/brain/targets", {"learner": "system-learning", **body},
                           content_type="application/json")


# ---- E: presets -------------------------------------------------------------------------------------------

def test_system_learning_ships_with_ada_and_brain_learn(w):
    assert (w["system"].operator, w["system"].entry) == (w["ada"], "/canopy:brain-learn")
    # Enabling it is the one switch; aiming it is the other. Nothing runs before a target.
    assert not BrainTarget.objects.exists()


def test_a_preset_never_overrides_a_chosen_operator(w):
    eva = Agent.objects.create(slug="eva", name="Eva", owner=w["jj"], workspace=w["dimagi"])
    BrainCapability.objects.filter(slug="system-learning").update(operator=eva, entry="/eva:learn")
    assert registry.apply_presets() == []
    cap = BrainCapability.objects.get(slug="system-learning")
    assert (cap.operator, cap.entry) == (eva, "/eva:learn")


def test_the_preset_migration_does_what_apply_presets_does():
    mig = importlib.import_module("apps.brain.migrations.0017_preset_system_learning")
    assert mig.PRESETS == registry.PRESETS


# ---- A: targets -------------------------------------------------------------------------------------------

def test_no_target_means_no_run(w):
    assert not AgentSchedule.objects.exists()
    schedule_clock.tick(now=timezone.now() + dt.timedelta(days=2))        # canopy's own clock: nothing to fire
    assert not BrainRun.objects.exists()


@pytest.mark.parametrize(("who", "ws", "where", "ok"), [
    ("ca", "connect", "", True),                      # its own workspace
    ("jj", "dimagi", "connect", True),                # an ancestor's owner, for a workspace below
    ("jj", "dimagi", "", True),
    ("ca", "connect", "dimagi", False),               # never above their own workspace
    ("la", "labs", "connect", False),                 # never beside it
    ("ce", "connect", "", False),                     # an editor manages nothing
])
def test_who_may_aim_a_learner_where(w, who, ws, where, ok):
    r = _add(w[who], ws, workspace=where)
    assert (r.status_code == 200) is ok, r.content
    assert BrainTarget.objects.exists() is ok


def test_a_target_is_a_normal_schedule_on_the_operator_beside_its_others(w):
    r = _add(w["ca"], "connect", cron="30 7 * * 1-5", timezone="Europe/London")
    assert r.status_code == 200, r.content
    t = BrainTarget.objects.get(pk=r.json()["id"])
    s = t.schedule
    assert (s.agent, s.cron, s.timezone, s.kind, s.enabled) == (w["ada"], "30 7 * * 1-5", "Europe/London", "cron", True)
    assert s.created_by == w["jj"] and t.created_by == w["ca"]           # fires as the learner's accountable admin
    assert "connect" in s.name
    listed = _client(w["jj"]).get("/api/agents/ada/schedules/").json()
    assert s.pk in [x["id"] for x in listed["items"]]                    # beside ada's other schedules
    assert SettingChange.objects.filter(operation="add_learning_target", workspace_id="connect").exists()


def test_a_project_target_reads_only_its_project_s_core_sessions(w):
    project = AgentProject.objects.create(agent=w["ada"], ext_id="P1", name="Supply", handle="supply",
                                          workspace=w["connect"], status=AgentProject.ACTIVE)
    other = Session.objects.create(workspace=w["connect"], created_by=w["alice"], origin=Session.ORIGIN_WEB, title="x")
    associate.put(sources.for_session(w["session"]), project, how="dispatched", tier="core")
    sources.for_session(other)
    r = _add(w["ca"], "connect", project="supply")
    assert r.status_code == 200, r.content
    [run] = fire(BrainTarget.objects.get(pk=r.json()["id"]))
    assert (run.subject_type, run.subject, run.scope) == ("project", str(project.pk), "connect")
    assert [s.session_id for s in reading.candidates(run)] == [w["session"].pk]


@pytest.mark.parametrize(("slug", "project", "words"), [
    ("project-learning", False, "aim it at a project"),
    ("coaching", False, "one person"),
    ("project-readers", False, "built in"),
])
def test_what_a_learner_may_be_aimed_at(w, slug, project, words):
    cap = BrainCapability.objects.get(slug=slug)
    BrainCapability.objects.filter(pk=cap.pk).update(operator=w["ada"], entry="/x:y", registered_by=w["jj"])
    r = _client(w["jj"]).post("/api/w/dimagi/brain/targets", {"learner": slug}, content_type="application/json")
    assert r.status_code == 422 and words in r.json()["detail"]


def test_a_learner_without_an_operator_or_an_admin_takes_no_target(w):
    BrainCapability.objects.filter(slug="system-learning").update(operator=None)
    assert "no operator" in _add(w["jj"], "dimagi").json()["detail"]
    BrainCapability.objects.filter(slug="system-learning").update(operator=w["ada"], registered_by=None)
    assert "enable it first" in _add(w["jj"], "dimagi").json()["detail"]


def test_an_operator_must_live_where_its_targets_are_or_above(w):
    lab = Agent.objects.create(slug="lab", name="Lab", owner=w["jj"], workspace=w["labs"])
    BrainCapability.objects.filter(slug="system-learning").update(operator=lab)
    r = _add(w["jj"], "dimagi", workspace="connect")
    assert r.status_code == 422 and "operator lives outside" in r.json()["detail"]
    BrainCapability.objects.filter(slug="system-learning").update(operator=w["ada"])
    assert _add(w["jj"], "dimagi", workspace="connect").status_code == 200
    # Moving the operator beside a target, or taking it away, is refused while the target stands.
    r = _client(w["jj"]).patch("/api/w/dimagi/brain/learners/system-learning/", {"operator": "lab"},
                               content_type="application/json")
    assert r.status_code == 422 and "connect" in r.json()["detail"]
    r = _client(w["jj"]).patch("/api/w/dimagi/brain/learners/system-learning/", {"operator": ""},
                               content_type="application/json")
    assert r.status_code == 422 and "target" in r.json()["detail"]


def test_a_new_operator_takes_the_targets_schedules_with_it(w):
    t = aim(w["system"], w["connect"])
    eva = Agent.objects.create(slug="eva", name="Eva", owner=w["jj"], workspace=w["dimagi"])
    r = _client(w["jj"]).patch("/api/w/dimagi/brain/learners/system-learning/", {"operator": "eva"},
                               content_type="application/json")
    assert r.status_code == 200, r.content
    t.schedule.refresh_from_db()
    assert t.schedule.agent == eva


def test_removing_a_target_removes_its_schedule_and_deleting_the_schedule_the_target(w):
    one, two = aim(w["system"], w["connect"]), aim(w["system"], w["labs"])
    r = _client(w["ca"]).delete(f"/api/w/connect/brain/targets/{one.pk}")
    assert r.status_code == 204
    assert not BrainTarget.objects.filter(pk=one.pk).exists() and not AgentSchedule.objects.filter(
        pk=one.schedule_id).exists()
    assert SettingChange.objects.filter(operation="remove_learning_target", workspace_id="connect").exists()
    assert _client(w["ca"]).delete(f"/api/w/connect/brain/targets/{two.pk}").status_code == 404   # beside: not theirs
    AgentSchedule.objects.filter(pk=two.schedule_id).delete()            # from the agent's schedules page
    assert not BrainTarget.objects.exists()


def test_the_page_lists_targets_with_their_schedule_and_last_run(w):
    t = aim(w["system"], w["connect"])
    body = _client(w["ca"]).get("/api/w/connect/brain/state").json()
    [row] = body["targets"]
    assert (row["id"], row["learner"], row["label"], row["agent"], row["cron"], row["can_manage"]) == (
        t.pk, "system-learning", "connect", "ada", targets.DEFAULT_CRON, True)
    assert row["last_run"] is None
    assert next(x for x in body["learners"] if x["slug"] == "system-learning")["targets"] == 1
    member_view = _client(w["alice"]).get("/api/w/connect/brain/state").json()["targets"]
    assert [x["can_manage"] for x in member_view] == [False]


# ---- B: canopy's scheduler fires a target --------------------------------------------------------------------

def test_a_fire_starts_one_run_per_slot_and_consumes_the_slot(w):
    t = aim(w["system"], w["connect"])
    slot = (timezone.now() + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
    turn, created = schedule_turns.fire_schedule(t.schedule, slot)
    assert turn is None and created is True                              # a run, not a plain turn
    run = BrainRun.objects.get(target=t)
    assert (run.slot, run.scope, run.subject) == (slot, "connect", "connect")
    [rt] = Turn.objects.filter(chat_session=run.session)
    assert rt.origin_ref["schedule_id"] == t.schedule_id and rt.origin_ref["slot"] == slot.isoformat()
    assert schedule_turns.latest_occurrence_turn(t.schedule) == rt     # in the schedule's own history
    # The same slot again (the clock and a runner both report it) is the same run.
    _turn, again = schedule_turns.fire_schedule(t.schedule, slot)
    assert again is False and BrainRun.objects.count() == 1
    assert schedule_hooks.announced_run(t.schedule, slot) == run
    t.schedule.refresh_from_db()
    assert t.schedule.last_slot == slot


def test_a_fire_with_nothing_new_or_reads_off_starts_nothing_but_still_consumes_the_slot(w):
    t = aim(w["system"], w["labs"])                                       # labs has no session
    slot = (timezone.now() + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
    schedule_turns.fire_schedule(t.schedule, slot)
    t.schedule.refresh_from_db()
    assert not BrainRun.objects.exists() and t.schedule.last_slot == slot
    c = aim(w["system"], w["connect"])
    reads(w["connect"], False)
    assert fire(c) == []
    reads(w["connect"], None)                                             # inherits dimagi's on again
    assert len(fire(c)) == 1


def test_one_open_run_per_target_and_each_target_its_own(w):
    c, d = aim(w["system"], w["connect"]), aim(w["system"], w["dimagi"])
    first = fire(c, d)
    assert sorted(r.scope for r in first) == ["connect", "dimagi"]
    assert fire(c, d) == []                                               # each still open


def test_run_now_on_the_agent_s_schedules_page_is_the_same_run(w):
    t = aim(w["system"], w["connect"])
    r = _client(w["jj"]).post(f"/api/agents/ada/schedules/{t.schedule_id}/run-now")
    assert r.status_code == 202, r.content
    run = BrainRun.objects.get(target=t)
    assert run.slot is None and run.state == BrainRun.OPEN


# ---- B: the brain's own scheduler is gone ---------------------------------------------------------------------

def test_the_brain_scheduler_is_gone_without_a_shim():
    from apps.common import ticks

    assert "brain.schedule" not in ticks.jobs()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("apps.brain.schedule")
    assert not hasattr(registry, "SCHEDULES")
    assert "schedule" not in {f.name for f in BrainCapability._meta.get_fields()}
    from apps.brain import models
    assert not hasattr(models, "BrainCursor")
    with pytest.raises(TypeError):
        registry.register(slug="x", name="x", purpose_class="system", purposes=["x"], schedule="daily")


def test_a_schedule_sent_to_a_learner_is_refused(w):
    r = _client(w["jj"]).patch("/api/w/dimagi/brain/learners/system-learning/", {"schedule": "daily"},
                               content_type="application/json")
    assert r.status_code == 422
    root = user("root")
    root.is_superuser = True
    root.save()
    r = _client(root).patch("/api/brain/capabilities/system-learning/", {"schedule": "daily"},
                            content_type="application/json")
    assert r.status_code == 422
