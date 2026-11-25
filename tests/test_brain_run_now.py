"""Running a learner's target now (`POST /api/w/<ws>/brain/targets/{id}/runs`), and the sessions an output cites
(`GET /api/brain/outputs/`).

Run now is the target's schedule's own Run now, a manager's as a person or an admin agent in its own turn
(`brain.actor`, `tests/test_brain_agent_admin.py`; never an agent's stored token, an unverified turn's or an act-as
token), and refuses with the reason when it cannot start. A `system` run is one run over every source eligible for `system`. An output
names the sessions it cites only where the viewer reads them; the rest are a count."""
from __future__ import annotations


import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.brain import associate, registry, sources
from apps.brain.models import (
    BrainCapability,
    BrainOutput,
    BrainRun,
    BrainTarget,
    LearningConsent,
    OutputLineage,
)
from apps.canopy_sessions.models import Message, Session
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.mcp import api_tools
from tests.fixtures.brain_world import aim, brain_world, member, reads, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db


def _client(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w(settings):
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
    out["target"] = aim(out["system"], out["ws"])
    return out


def _run(w, who="root", target=None):
    t = target or w["target"]
    return _client(w[who]).post(f"/api/w/acme/brain/targets/{t.pk}/runs", {}, content_type="application/json")


def test_run_now_starts_one_system_run_for_its_target_and_refuses_a_second(w):
    r = _run(w)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["subject_type"] == "system" and body["subject"] == "system" and body["state"] == "open"
    run = BrainRun.objects.get(pk=body["id"])
    assert run.subject_type == "system" and run.subject == "acme" and run.scope == "acme" and run.session is not None
    assert run.target == w["target"] and run.slot is None and body["scope"] == "acme"
    [turn] = Turn.objects.filter(chat_session=run.session)
    # It is its schedule's own Run now: an occurrence of that schedule, so the schedule's history lists it.
    assert turn.origin_ref["schedule_id"] == w["target"].schedule_id and turn.origin_ref["manual"] is True
    again = _run(w)
    assert again.status_code == 422 and f"run {run.pk}" in again.json()["detail"]
    # What it reads is every source eligible for system: the shared session, whose people both agreed.
    from apps.brain import reading
    assert [s.session_id for s in reading.candidates(run)] == [w["session"].pk]


@pytest.mark.parametrize(("change", "words"), [
    ({"gate": False}, "learning reads are off"),
    ({"enabled": False}, "disabled"),
    ({"operator": None}, "no operator"),
    ({"entry": ""}, "no entry"),
])
def test_run_now_says_why_it_cannot_start(w, settings, change, words):
    if change.pop("gate", True) is False:
        reads(w["ws"], False)
    BrainCapability.objects.filter(slug="system-learning").update(**change)
    r = _run(w)
    assert r.status_code == 422 and words in r.json()["detail"]
    assert not BrainRun.objects.exists()


def test_run_now_needs_something_new(w):
    BrainRun.objects.create(capability=w["system"], subject_type="system", subject="acme", scope="acme",
                            purpose="improve", state=BrainRun.DONE)
    from apps.brain.models import BrainRunRead
    BrainRunRead.objects.create(run=BrainRun.objects.get(), source=sources.for_session(w["session"]))
    r = _run(w)
    assert r.status_code == 422 and "nothing new" in r.json()["detail"]


def test_the_run_answers_to_the_learner_s_admin_and_a_person_learner_takes_no_target(w):
    w["root"].is_active = False                                         # the learner's own admin is gone
    w["root"].save(update_fields=["is_active"])
    r = _run(w, who="owner")
    assert r.status_code == 422 and "deactivated" in r.json()["detail"]   # its schedule fires as nobody now
    # The workspace owner enables it again: they answer for it, and its targets' schedules follow them.
    assert _client(w["owner"]).patch("/api/w/acme/brain/learners/system-learning/", {"enabled": True},
                                     content_type="application/json").status_code == 200
    w["target"].schedule.refresh_from_db()
    assert w["target"].schedule.created_by == w["owner"]
    r = _run(w, who="owner")
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(chat_session=BrainRun.objects.get(pk=r.json()["id"]).session)
    assert turn.initiator_user_id == w["owner"].pk
    r = _client(w["owner"]).post("/api/w/acme/brain/targets", {"learner": "coaching"}, content_type="application/json")
    assert r.status_code == 422 and "one person" in r.json()["detail"]


def test_a_project_learner_takes_a_project_target(w):
    proj = BrainCapability.objects.get(slug="project-learning")
    registry.update(proj, by=w["root"], operator=w["operator"], entry="/canopy:brain-learn", enabled=True)
    project = AgentProject.objects.create(agent=w["operator"], ext_id="P1", name="Supply", handle="supply",
                                          workspace=w["ws"], status=AgentProject.ACTIVE)
    add = lambda **b: _client(w["root"]).post("/api/w/acme/brain/targets", {"learner": "project-learning", **b},  # noqa: E731
                                              content_type="application/json")
    assert add().status_code == 422                                       # a project learner needs a project
    assert add(project="nope").status_code == 422
    r = add(project="supply")
    assert r.status_code == 200, r.content
    assert r.json()["project"] == "supply" and r.json()["label"] == "acme/supply"
    associate.put(sources.for_session(w["session"]), project, how="dispatched", tier="core")
    LearningConsent.objects.filter(user__in=[w["alice"], w["bob"]]).update(classes=["system", "project"])
    run = _run(w, target=BrainTarget.objects.get(pk=r.json()["id"]))
    assert run.status_code == 200, run.content
    assert run.json()["subject"] == "supply"
    assert BrainRun.objects.get(pk=run.json()["id"]).subject == str(project.pk)


def test_only_a_manager_never_an_agent_without_one_behind_it(w):
    assert _run(w, who="alice").status_code == 403                                     # a member, not a manager
    w["operator"].user.is_superuser = True                                              # even a superuser agent login
    w["operator"].user.save()
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/runs"
    r = Client().post(url, {}, content_type="application/json", **bearer_headers(pat_token(w["operator"].user)))
    assert r.status_code == 403
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=w["operator"], prompt="x", idempotency_key="k2", status=Turn.RUNNING,
                               claimed_by=box, initiator_kind="user", initiator_user=w["root"])
    token = turn_agent_tokens.issue(turn, box)["token"]
    r = Client().post(url, {}, content_type="application/json", **bearer_headers(token))
    assert r.status_code == 403
    assert _client(w["root"]).post(url, {}, content_type="application/json",
                                   HTTP_X_CANOPY_TURN_ID="x").status_code == 403
    assert "run_learning_target" not in api_tools.EXCLUDED                             # a tool: the rule is the route's
    assert act_as.route_refusal("POST", f"/api/brain/targets/{w['target'].pk}/runs", "write",
                                is_tool=False) is not None
    assert not BrainRun.objects.exists()


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
    assert row["source_sessions"] == []                                         # so no evidence quote links anywhere
    w["owner"].refresh_from_db()
    rows = _client(w["owner"]).get("/api/brain/outputs/").json()                  # the workspace owner
    row = next(r for r in rows if r["id"] == out.pk)
    assert row["hidden_sessions"] + len(row["sessions"]) == 2
    assert all(set(s) == {"session_id", "workspace", "title"} for s in row["sessions"])
    # Evidence names a source id; only a session the viewer already reads gets mapped to it, and only its own source's.
    src = {str(s.pk): sources.for_session(s).pk for s in (seen, unseen)}
    assert {x["session_id"] for x in row["source_sessions"]} == {s["session_id"] for s in row["sessions"]}
    assert all(x["source"] == src[x["session_id"]] for x in row["source_sessions"])
