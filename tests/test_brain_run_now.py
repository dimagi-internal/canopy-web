"""Running a learner now (`POST /api/brain/capabilities/{slug}/runs/`), the `system` subject on the schedule, and the
sessions an output cites (`GET /api/brain/outputs/`).

Run now is a canopy admin's, as a person (never an agent's login, a turn's token, an act-as token or MCP), and refuses
with the reason when it cannot start. A `system` run is one run over every source eligible for `system`. An output
names the sessions it cites only where the viewer reads them; the rest are a count."""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.brain import registry, schedule, sources
from apps.brain.models import BrainCapability, BrainCursor, BrainOutput, BrainRun, LearningConsent, OutputLineage
from apps.canopy_sessions.models import Message, Session
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.mcp import api_tools
from tests.fixtures.brain_world import brain_world, member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db


def _client(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = True
    out = brain_world()
    root = user("root")
    root.is_superuser = True
    root.save()
    member(root, out["ws"])
    operator = Agent.objects.create(slug="ada", name="Ada", owner=out["owner"], workspace=out["ws"],
                                    user=user("ada_login"))
    member(operator.user, out["ws"])
    Message.objects.create(session=out["session"], role=Message.USER, turn_index=0, plaintext="the runner idles",
                           author={"user_id": out["alice"].pk})
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    registry.ensure_learners()                       # what migration 0007 seeds; this database has no migrations
    system = BrainCapability.objects.get(slug="system-learning")
    registry.update(system, by=root, operator=operator, entry="/canopy:brain-learn", enabled=True)
    out.update(root=root, operator=operator, system=BrainCapability.objects.get(slug="system-learning"))
    return out


def _run(w, slug="system-learning", who="root", **body):
    return _client(w[who]).post(f"/api/w/acme/brain/capabilities/{slug}/runs/", body, content_type="application/json")


def test_run_now_starts_one_system_run_and_refuses_a_second(w):
    r = _run(w)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["subject_type"] == "system" and body["subject"] == "system" and body["state"] == "open"
    run = BrainRun.objects.get(pk=body["id"])
    assert run.subject_type == "system" and run.subject == "acme" and run.scope == "acme" and run.session is not None
    assert body["scope"] == "acme"
    assert Turn.objects.filter(chat_session=run.session).count() == 1
    again = _run(w)
    assert again.status_code == 422 and f"run {run.pk}" in again.json()["detail"]
    # What it reads is every source eligible for system: the shared session, whose people both agreed.
    from apps.brain import reading
    assert [s.session_id for s in reading.candidates(run)] == [w["session"].pk]


@pytest.mark.parametrize(("change", "words"), [
    ({"gate": False}, "switched off"),
    ({"enabled": False}, "disabled"),
    ({"operator": None}, "no operator"),
    ({"entry": ""}, "no entry"),
])
def test_run_now_says_why_it_cannot_start(w, settings, change, words):
    if change.pop("gate", True) is False:
        settings.BRAIN_READS_ENABLED = False
    BrainCapability.objects.filter(slug="system-learning").update(**change)
    r = _run(w)
    assert r.status_code == 422 and words in r.json()["detail"]
    assert not BrainRun.objects.exists()


def test_the_starter_answers_for_the_run_and_a_person_learner_is_refused(w):
    w["root"].is_active = False                                         # the learner's own admin is gone
    w["root"].save(update_fields=["is_active"])
    r = _client(w["owner"]).post("/api/w/acme/brain/capabilities/system-learning/runs/", {},
                                 content_type="application/json")
    assert r.status_code == 200, r.content                               # the workspace owner who started it answers
    turn = Turn.objects.get(chat_session=BrainRun.objects.get(pk=r.json()["id"]).session)
    assert turn.initiator_user_id == w["owner"].pk
    coaching = BrainCapability.objects.get(slug="coaching")
    r = _client(w["owner"]).post(f"/api/w/acme/brain/capabilities/{coaching.slug}/runs/", {},
                                 content_type="application/json")
    assert r.status_code == 422 and "one person" in r.json()["detail"]


def test_a_project_learner_takes_a_project_handle(w):
    proj = BrainCapability.objects.get(slug="project-learning")
    registry.update(proj, by=w["root"], operator=w["operator"], entry="/canopy:brain-learn", enabled=True)
    project = AgentProject.objects.create(agent=w["operator"], ext_id="P1", name="Supply", handle="supply",
                                          workspace=w["ws"])
    assert _run(w, "project-learning", project="nope").status_code == 422
    r = _run(w, "project-learning", project="supply")
    assert r.status_code == 200, r.content
    assert r.json()["subject"] == "supply"
    assert BrainRun.objects.get(pk=r.json()["id"]).subject == str(project.pk)


def test_only_a_canopy_admin_as_a_person(w):
    assert _run(w, who="alice").status_code == 403                                     # not a canopy admin
    w["operator"].user.is_superuser = True                                              # even a superuser agent login
    w["operator"].user.save()
    r = Client().post("/api/w/acme/brain/capabilities/system-learning/runs/", {}, content_type="application/json",
                      **bearer_headers(pat_token(w["operator"].user)))
    assert r.status_code == 403
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=w["operator"], prompt="x", idempotency_key="k2", status=Turn.RUNNING,
                               claimed_by=box, initiator_kind="user", initiator_user=w["root"])
    token = turn_agent_tokens.issue(turn, box)["token"]
    r = Client().post("/api/w/acme/brain/capabilities/system-learning/runs/", {}, content_type="application/json",
                      **bearer_headers(token))
    assert r.status_code == 403
    assert _client(w["root"]).post("/api/w/acme/brain/capabilities/system-learning/runs/", {}, content_type="application/json",
                                   HTTP_X_CANOPY_TURN_ID="x").status_code == 403
    assert "run_brain_capability" in api_tools.EXCLUDED
    assert act_as.route_refusal("POST", "/api/brain/capabilities/system-learning/runs/", "write",
                                is_tool=False) is not None
    assert not BrainRun.objects.exists()


def test_the_schedule_runs_a_system_learner_once_per_interval(w):
    BrainCapability.objects.filter(slug="system-learning").update(schedule="daily")
    started = schedule.run_due()
    assert [(r.capability.slug, r.subject_type, r.subject, r.scope) for r in started] == [
        ("system-learning", "system", "acme", "acme")]
    assert BrainCursor.objects.filter(capability__slug="system-learning", subject_type="system").exists()
    BrainRun.objects.update(state=BrainRun.DONE)
    assert schedule.run_due() == []                                      # within the interval
    later = timezone.now() + dt.timedelta(days=2)
    BrainRun.objects.all().delete()                                      # nothing learned yet: still something new
    assert len(schedule.run_due(now=later)) == 1


def test_an_output_names_only_the_sessions_the_viewer_reads(w):
    seen = w["session"]
    unseen = Session.objects.create(workspace=w["ws"], created_by=w["carol"], origin=Session.ORIGIN_WEB, title="c")
    out = BrainOutput.objects.create(capability=w["system"], kind="finding", workspace=w["ws"], subject_type="system",
                                     subject="", title="the runner idles")
    for s in (seen, unseen):
        OutputLineage.objects.create(output_id=str(out.pk), source=sources.for_session(s))
    rows = _client(w["root"]).get("/api/brain/outputs/").json()                  # a canopy admin sees system findings
    row = next(r for r in rows if r["id"] == out.pk)
    assert row["sessions"] == [] and row["hidden_sessions"] == 2                # but reads neither session
    w["owner"].refresh_from_db()
    rows = _client(w["owner"]).get("/api/brain/outputs/").json()                  # the workspace owner
    row = next(r for r in rows if r["id"] == out.pk)
    assert row["hidden_sessions"] + len(row["sessions"]) == 2
    assert all(set(s) == {"session_id", "workspace", "title"} for s in row["sessions"])
