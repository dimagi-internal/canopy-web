"""A learning run is a full-profile turn of its operator, set up by the learning system, isolated, with lineage over
everything it makes.

C. The run's turn is the operator's whole profile (no confined `brain` profile). The learning system gives it its
   context (the learner, the target, who it answers to, the run, the sessions due) in its prompt and its caller
   envelope. It reads the sessions due only through `brain_read`; its own conversation is readable by no other turn;
   and it runs only on a runner that keeps nothing of it for later turns (`isolated`).
D. Everything the turn creates in canopy is recorded against the sources it had read (`BrainRunArtifact` +
   `OutputLineage`), automatically from its credential; links it reports are recorded at its end. A removal withdraws
   what canopy can undo and flags the rest, on the brain page and to the item's owner."""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask, ProjectUpdate
from apps.attention import alerts
from apps.attention.models import Alert
from apps.brain import alerts_removal, cascade, consent, lineage, reading, registry, runs, targets
from apps.brain.models import BrainCapability, BrainRunArtifact, LearningConsent, OutputLineage
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.harness import caller_context, claim, isolation, services, turn_agent_tokens
from apps.harness.models import Runner, Turn
from tests.fixtures.brain_world import brain_world, member, user

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture()
def w():
    out = brain_world()
    ws, owner = out["ws"], out["owner"]
    login = user("ada_login")
    member(login, ws)
    ada = Agent.objects.create(slug="ada", name="Ada", owner=owner, workspace=ws, user=login)
    registry.ensure_learners()                                            # system-learning preset: ada
    system = BrainCapability.objects.get(slug="system-learning")
    registry.update(system, by=owner, enabled=True)
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["system"])
    Message.objects.create(session=out["session"], role=Message.USER, turn_index=0, plaintext="the runner idles",
                           author={"user_id": out["alice"].pk})
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=ws, status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now(), capabilities={"isolated": isolation.ISOLATED_VERSION})
    target = targets.add(BrainCapability.objects.get(pk=system.pk), workspace=ws, by=owner)
    run, why = targets.start(target)
    assert run is not None, why
    turn = Turn.objects.get(chat_session=run.session)
    return {**out, "ada": ada, "box": box, "target": target, "run": run, "turn": turn}


def _running(w):
    Turn.objects.filter(pk=w["turn"].pk).update(status=Turn.RUNNING, claimed_by=w["box"])
    w["turn"].refresh_from_db()
    return turn_agent_tokens.issue(w["turn"], w["box"])["token"]


def _rest(raw, method, path, **kw):
    return getattr(Client(), method)(path, HTTP_AUTHORIZATION=f"Bearer {raw}", **kw)


def _read(raw):
    r = _rest(raw, "get", "/api/brain/run/read?limit=5")
    assert r.status_code == 200, r.content
    return r.json()


# ---- C: a full-profile turn, set up by the learning system ---------------------------------------------------

def test_the_run_s_turn_is_the_operator_s_whole_profile(w):
    from apps.agents import turn_context

    turn = w["turn"]
    assert turn.capability == "" and turn.initiator_user_id == w["owner"].pk
    ctx = turn_context.for_turn(turn, w["ada"])
    assert ctx.access == turn_context.FULL and ctx.profile.kind == turn_context.WHOLE_AGENT


def test_the_learning_system_gives_the_turn_its_context(w):
    env = caller_context.build(w["turn"])["brain_run"]
    assert env["run"] == w["run"].pk and env["learner"] == "system-learning" and env["purpose"] == "improve"
    assert env["target"] == {"id": w["target"].pk, "kind": "workspace", "workspace": "acme", "project": "",
                             "schedule_id": w["target"].schedule_id}
    assert env["accountable"]["email"] == w["owner"].email
    assert env["sessions_due"] == 1 and env["source_ids_due"] == [w["source"].pk]
    assert "brain_read" in env["how"]
    assert "Target: the workspace acme" in w["turn"].prompt and "Sessions due: 1" in w["turn"].prompt
    assert w["run"].session.metadata == {"brain_run": str(w["run"].pk), "brain_target": w["target"].pk}
    # Any other turn has none.
    chat = Session.objects.create(workspace=w["ws"], agent=w["ada"], created_by=w["alice"])
    from apps.harness import initiator
    other, _ = services.enqueue_turn(session=chat, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hi",
                                     idempotency_key="other", initiator=initiator.for_user(w["alice"], via="chat", assurance="session"))
    assert caller_context.build(other)["brain_run"] is None


def test_the_sessions_due_are_read_only_through_brain_read(w):
    raw = _running(w)
    assert _rest(raw, "get", f"/api/canopy-sessions/{w['session'].pk}").status_code == 404
    page = _read(raw)
    assert [s["source"] for s in page["sources"]] == [w["source"].pk]


def test_the_run_s_conversation_is_read_by_no_other_turn_of_the_operator(w):
    raw = _running(w)
    _read(raw)
    # The operator's ordinary chat: its login and its asker reach their own work, never the run's conversation.
    chat = Session.objects.create(workspace=w["ws"], agent=w["ada"], created_by=w["owner"])
    from apps.harness import initiator
    other, _ = services.enqueue_turn(session=chat, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="what did you learn?",
                                     idempotency_key="o2", initiator=initiator.for_user(w["owner"], via="chat", assurance="session"))
    Turn.objects.filter(pk=other.pk).update(status=Turn.RUNNING, claimed_by=w["box"])
    other.refresh_from_db()
    other_login = turn_agent_tokens.issue(other, w["box"])["token"]
    assert _rest(other_login, "get", f"/api/canopy-sessions/{w['run'].session_id}").status_code == 404
    assert not access.can_read(w["owner"], w["run"].session, brain=True)
    assert not access.can_read(w["ada"].user, w["run"].session, brain=True)


def test_a_run_s_turn_is_isolated_and_only_a_runner_that_isolates_takes_it(w):
    from apps.harness.schemas.turns import ClaimedTurnOut

    assert isolation.is_isolated(w["turn"])
    assert ClaimedTurnOut.resolve_isolated(w["turn"]) is True
    laptop = Runner.objects.create(name="laptop", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    assert "keeps nothing" in isolation.refusal(laptop, w["turn"])
    # A box on code that turns auto memory off but keeps the run's Claude session on disk (level 1) takes none.
    older = Runner.objects.create(name="older", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={"isolated": 1})
    assert "keeps nothing" in isolation.refusal(older, w["turn"])
    assert isolation.refusal(w["box"], w["turn"]) is None
    # The claim passes the old runner over, even pinned to it (above the pin), and the isolating one takes it.
    Turn.objects.filter(pk=w["turn"].pk).update(pinned_runner=laptop)
    assert claim.claim_next_turn(laptop) is None
    assert Turn.objects.get(pk=w["turn"].pk).status == Turn.QUEUED
    Turn.objects.filter(pk=w["turn"].pk).update(pinned_runner=w["box"])
    got = claim.claim_next_turn(w["box"])
    assert got is not None and got.pk == w["turn"].pk


def test_a_runner_reports_that_it_isolates_on_its_heartbeat(w):
    laptop = Runner.objects.create(name="old", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    services.heartbeat(laptop, active_turn_ids=[], isolated=1)
    laptop.refresh_from_db()
    assert laptop.capabilities["isolated"] == 1
    services.heartbeat(laptop, active_turn_ids=[])                         # an older runner: absent is 0
    laptop.refresh_from_db()
    assert laptop.capabilities["isolated"] == 0


# ---- D: lineage over everything the run makes ---------------------------------------------------------------

def test_what_the_run_creates_is_recorded_against_what_it_read(w):
    raw = _running(w)
    _read(raw)
    r = _rest(raw, "post", "/api/tasks/", data={"title": "Fix the idle runner", "workspace": "acme"}, content_type=J)
    assert r.status_code == 201, r.content
    task = AgentTask.objects.get(title="Fix the idle runner")
    art = BrainRunArtifact.objects.get(kind="agents.agenttask", ref=str(task.pk))
    assert (art.run, art.workspace_id, art.owner, art.state) == (w["run"], "acme", w["owner"], BrainRunArtifact.LIVE)
    assert list(OutputLineage.objects.filter(output_kind=lineage.ARTIFACT_KIND, output_id=str(art.pk))
                .values_list("source_id", flat=True)) == [w["source"].pk]


def test_another_turn_s_work_and_a_refused_write_are_not_recorded(w):
    raw = _running(w)
    _read(raw)
    assert _rest(raw, "post", "/api/tasks/", data={"title": "x", "workspace": "nowhere"}, content_type=J).status_code \
        in (404, 422)
    c = Client()
    c.force_login(w["owner"])
    assert c.post("/api/tasks/", {"title": "mine", "workspace": "acme"}, content_type=J).status_code == 201
    assert not BrainRunArtifact.objects.exists()


def test_links_the_run_reports_are_recorded_at_its_end(w):
    _running(w)
    Message.objects.create(session=w["run"].session, role=Message.ASSISTANT, turn_index=1,
                           plaintext="Opened https://github.com/dimagi-internal/canopy/pull/77 and filed "
                                     "https://github.com/dimagi-internal/canopy/issues/9, notes at https://example.org/x.")
    Turn.objects.filter(pk=w["turn"].pk).update(result_note="see https://canopy.dimagi.com/w/acme/brain")
    runs.finish(w["run"], ok=True)
    got = set(BrainRunArtifact.objects.values_list("kind", "ref"))
    assert got == {("github.pull", "https://github.com/dimagi-internal/canopy/pull/77"),
                   ("github.issue", "https://github.com/dimagi-internal/canopy/issues/9"),
                   ("link", "https://example.org/x")}                       # canopy's own page is not a link it made


def test_a_removal_withdraws_what_it_can_and_flags_the_rest_to_the_owner_and_the_page(w):
    raw = _running(w)
    _read(raw)
    _rest(raw, "post", "/api/tasks/", data={"title": "Fix the idle runner", "workspace": "acme"}, content_type=J)
    task = AgentTask.objects.get(title="Fix the idle runner")
    run = w["run"]
    pr = lineage.record(run, "github.pull", "https://github.com/dimagi-internal/canopy/pull/77")
    project = AgentProject.objects.create(agent=w["ada"], ext_id="P1", name="Supply", handle="supply")
    update = ProjectUpdate.objects.create(project=project, body="from the session", status=ProjectUpdate.PUBLISHED,
                                           health=ProjectUpdate.ON_TRACK)
    published = lineage.record(run, "agents.projectupdate", str(update.pk))
    consent.exclude(w["alice"], w["source"])
    cascade.sweep()
    assert not AgentTask.objects.filter(pk=task.pk).exists()             # undone
    assert BrainRunArtifact.objects.get(kind="agents.agenttask", ref=str(task.pk)).state == BrainRunArtifact.WITHDRAWN
    for art in (pr, published):
        art.refresh_from_db()
        assert art.state == BrainRunArtifact.FLAGGED and "removed" in art.reason
    assert ProjectUpdate.objects.filter(pk=update.pk).exists()           # a published update is flagged, not deleted
    # Raised to its owner (the person the run answered to) as an alert, and listed on the brain page.
    alerts.apply_reconcile(alerts_removal.KIND, alerts_removal.reconcile(timezone.now()), timezone.now())
    raised = Alert.objects.filter(kind=alerts_removal.KIND, status=Alert.OPEN)
    assert raised.count() == 2 and set(raised.values_list("handler_user_id", flat=True)) == {w["owner"].pk}
    c = Client()
    c.force_login(w["owner"])
    flags = c.get("/api/w/acme/brain/state").json()["flags"]
    assert {f["kind"] for f in flags} == {"github.pull", "agents.projectupdate"}
    member_view = Client()
    member_view.force_login(w["carol"])
    assert member_view.get("/api/w/acme/brain/state").json()["flags"] == []
    # Acknowledging it closes the alert at the next reconcile.
    assert c.post(f"/api/w/acme/brain/flags/{pr.pk}/acknowledge").status_code == 204
    assert member_view.post(f"/api/w/acme/brain/flags/{published.pk}/acknowledge").status_code == 403
    alerts.apply_reconcile(alerts_removal.KIND, alerts_removal.reconcile(timezone.now()), timezone.now())
    assert Alert.objects.filter(kind=alerts_removal.KIND, status=Alert.OPEN).count() == 1


def test_a_mail_draft_the_run_made_is_undone_by_the_mail_app(w):
    from apps.mail.models import MailDraft

    draft = MailDraft.objects.create(uuid="00000000-0000-0000-0000-000000000001", workspace=w["ws"],
                                     user=w["ada"].user, subject="about the runner")
    _running(w)
    reading.read_next(w["run"], limit=5)
    art = lineage.record(w["run"], "mail.maildraft", str(draft.pk))
    assert lineage.undo(art, "a session it came from was removed") == BrainRunArtifact.WITHDRAWN
    assert not MailDraft.objects.filter(pk=draft.pk).exists()


def test_a_task_a_person_already_took_up_is_flagged_not_deleted(w):
    raw = _running(w)
    _read(raw)
    _rest(raw, "post", "/api/tasks/", data={"title": "Fix it", "workspace": "acme"}, content_type=J)
    task = AgentTask.objects.get(title="Fix it")
    AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.IN_PROGRESS)
    SessionParticipant.objects.filter(session=w["session"]).delete()
    consent.exclude(w["alice"], w["source"])
    cascade.sweep()
    assert AgentTask.objects.filter(pk=task.pk).exists()
    assert BrainRunArtifact.objects.get(kind="agents.agenttask", ref=str(task.pk)).state == BrainRunArtifact.FLAGGED
