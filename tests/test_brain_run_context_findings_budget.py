"""The run-context document, a finding's lifecycle and folding, "why is this session not read?", a target's budget,
and the one viewer scope the brain page, the graph and the outputs list share."""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import budget, context, emit, findings, reading, sources, targets
from apps.brain.models import BrainOutput, BrainRun, LearningConsent, OutputLineage
from apps.brain.schemas import BrainRunContext
from apps.canopy_sessions.models import Message, Session
from apps.events.models import SessionUsage
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.brain_world import aim, brain_world, member, reads, user

pytestmark = pytest.mark.django_db
J = "application/json"


def _chat(ws, who, text):
    s = Session.objects.create(workspace=ws, created_by=who, origin=Session.ORIGIN_WEB, title=f"{who.username} chat")
    Message.objects.create(session=s, role=Message.USER, turn_index=0, plaintext=text, author={"user_id": who.pk})
    sources.for_session(s)
    return s


def _c(u):
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w():
    out = brain_world()
    out.pop("session").delete()
    ws, owner = out["ws"], out["owner"]
    out["op"] = Agent.objects.create(slug="ada", name="Ada", owner=owner, workspace=ws, user=user("ada_login"))
    member(out["op"].user, ws)
    sys = out["sys"]
    sys.purposes, sys.output_kinds, sys.operator, sys.entry, sys.registered_by, sys.subject_type = (
        ["improve"], ["finding"], out["op"], "/canopy:brain-learn", owner, "system")
    sys.save()
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    out["chats"] = [_chat(ws, out["alice"] if i % 2 else out["bob"], f"line {i}") for i in range(4)]
    out["target"] = aim(sys, ws, by=owner)
    return out


def _start(w):
    run, why = targets.start(w["target"])
    assert run is not None, why
    return run


# ---- 1. a typed, versioned run context, kept on the run ---------------------------------------------------------

def test_a_run_keeps_the_typed_context_it_was_started_with(w):
    run = _start(w)
    doc = BrainRun.objects.get(pk=run.pk).context
    assert BrainRunContext.model_validate(doc).version == context.VERSION == 1
    assert doc["learner"] == "sys" and doc["sessions_due"] == 4 and doc["target"]["id"] == w["target"].pk
    assert doc["budget"]["max_sources_per_run"] == 50 and doc["accountable"]["email"] == w["owner"].email


def test_the_page_shows_a_run_s_context_without_the_ids_it_may_not_read(w):
    run = _start(w)
    body = _c(w["owner"]).get("/api/w/acme/brain/state/runs").json()
    shown = next(r for r in body["runs"] if r["id"] == run.pk)["context"]
    assert shown["version"] == 1 and shown["sessions_due"] == 4 and shown["source_ids_due"] == []


def test_the_envelope_is_the_same_document(w):
    from apps.harness.models import Turn

    run = _start(w)
    turn = Turn.objects.get(chat_session=run.session)
    doc = context.of_turn(turn)
    assert doc["version"] == 1 and doc["run"] == run.pk and "purpose_class" in doc


# ---- 2. a finding's lifecycle, and a repeat folding into the open one ---------------------------------------------

def _finding(w, run, key="cli:timeout"):
    page = reading.read_next(run, limit=5)
    return emit.emit(run, kind="finding", title="CLI times out", body={"what": "timeouts"},
                     source_ids=[page["sources"][0]["source"]], key=key)


def test_a_repeat_with_the_same_key_folds_into_the_open_finding(w):
    first = _start(w)
    out = _finding(w, first)
    assert out.folded is False and out.status == "open" and out.seen == 1
    from apps.brain import runs

    runs.finish(first, ok=True)
    _chat(w["ws"], w["alice"], "it timed out again")
    second = _start(w)
    again = _finding(w, second)
    assert again.folded is True and again.pk == out.pk
    out.refresh_from_db()
    assert out.seen == 2 and BrainOutput.objects.filter(key="cli:timeout").count() == 1
    # The repeat's reads are its lineage too: a removal of what the second run read still reaches it.
    assert OutputLineage.objects.filter(output_id=str(out.pk), source__in=second.reads.values("source")).exists()


def test_a_dismissed_finding_takes_no_repeat(w):
    from apps.brain import runs

    first = _start(w)
    out = _finding(w, first)
    findings.set_status(out, "dismissed", by=w["owner"])
    runs.finish(first, ok=True)
    _chat(w["ws"], w["alice"], "again")
    again = _finding(w, _start(w))
    assert again.folded is False and again.pk != out.pk


def test_the_owner_sets_a_finding_s_status_and_a_member_cannot(w):
    out = _finding(w, _start(w))
    url = f"/api/w/acme/brain/outputs/{out.pk}/status"
    assert _c(w["alice"]).post(url, data={"status": "accepted"}, content_type=J).status_code == 404   # not hers to see
    r = _c(w["owner"]).post(url, data={"status": "accepted"}, content_type=J)
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["status_by"] == w["owner"].email
    assert _c(w["owner"]).post(url, data={"status": "maybe"}, content_type=J).status_code == 422
    listed = _c(w["owner"]).get("/api/w/acme/brain/outputs/?status=accepted").json()
    assert [o["id"] for o in listed] == [out.pk] and listed[0]["may_set_status"] is True


# ---- 3. why a session is not read ------------------------------------------------------------------------------------

def test_why_names_you_or_someone_never_who(w):
    s = w["chats"][1]                                   # alice's
    from apps.canopy_sessions.models import SessionParticipant

    SessionParticipant.objects.create(session=s, user=w["carol"])          # carol never agreed
    sources.for_session(s)
    got = _c(w["alice"]).get(f"/api/w/acme/brain/why/{s.pk}?learner=sys").json()
    assert got["eligible"] is False and got["reason"] == "no_consent:someone" and "carol" not in str(got)
    mine = _c(w["carol"]).get(f"/api/w/acme/brain/why/{s.pk}?learner=sys").json()
    assert mine["reason"] == "no_consent:you"
    ok = _c(w["bob"]).get(f"/api/w/acme/brain/why/{w['chats'][0].pk}?learner=sys").json()
    assert ok["eligible"] is True and ok["reason"] == ""


def test_why_is_only_for_a_session_you_are_in_or_read(w):
    s = w["chats"][1]
    assert _c(w["carol"]).get(f"/api/w/acme/brain/why/{s.pk}").status_code == 404
    assert _c(w["owner"]).get(f"/api/w/acme/brain/why/{s.pk}").status_code == 404          # an owner reads no one's


def test_why_says_learning_reads_are_off(w):
    reads(w["ws"], False)
    got = _c(w["bob"]).get(f"/api/w/acme/brain/why/{w['chats'][0].pk}?learner=sys").json()
    assert got["reason"] == "learning_reads_off"


# ---- 4. a target's budget -------------------------------------------------------------------------------------------

def test_a_run_stops_reading_cleanly_at_its_budget_of_sessions(w):
    targets.set_budget(w["target"], max_sources_per_run=3)
    run = _start(w)
    page = reading.read_next(run, limit=5)
    assert len(page["sources"]) == 3 and page["done"] is True and page["budget"]["stopped"] == "sources"
    again = reading.read_next(run, after=page["next_after"], limit=5)
    assert again["sources"] == [] and again["done"] is True


def test_a_run_s_outputs_stop_at_its_budget_and_a_repeat_costs_nothing(w):
    targets.set_budget(w["target"], max_outputs_per_run=1)
    run = _start(w)
    page = reading.read_next(run, limit=5)
    src = page["sources"][0]["source"]
    emit.emit(run, kind="finding", title="one", body={"a": 1}, source_ids=[src], key="k1")
    with pytest.raises(emit.EmitRefusedError, match="budget"):
        emit.emit(run, kind="finding", title="two", body={"a": 2}, source_ids=[src], key="k2")
    assert emit.emit(run, kind="finding", title="one again", body={"a": 3}, source_ids=[src], key="k1").folded


def test_a_target_starts_no_run_past_its_daily_budget(w):
    from apps.brain import runs

    targets.set_budget(w["target"], max_runs_per_day=1)
    first = _start(w)
    runs.finish(first, ok=False)
    run, why = targets.start(w["target"])
    assert run is None and "1 run(s) a day" in why


def test_tokens_a_day_are_checked_before_a_run_starts(w):
    from apps.brain import runs

    targets.set_budget(w["target"], max_tokens_per_day=1000)
    first = _start(w)
    runs.finish(first, ok=False)
    SessionUsage.objects.create(claude_session="c1", day=timezone.now().date(), workspace=w["ws"], session=first.session,
                                input_tokens=900, output_tokens=200)
    assert budget.today(w["target"])["tokens"] == 1100
    run, why = targets.start(w["target"])
    assert run is None and "tokens a day" in why


def test_the_page_shows_and_the_owner_changes_a_budget(w):
    c = _c(w["owner"])
    shown = c.get("/api/w/acme/brain/state").json()["targets"][0]["budget"]
    assert shown["max_sources_per_run"] == 50 and shown["today"] == {"runs": 0, "outputs": 0, "tokens": 0}
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/budget"
    r = c.patch(url, data={"max_sources_per_run": 10, "max_tokens_per_day": 5000}, content_type=J)
    assert r.status_code == 200 and r.json()["budget"]["max_sources_per_run"] == 10
    assert c.patch(url, data={"clear_tokens": True}, content_type=J).json()["budget"]["max_tokens_per_day"] is None
    assert _c(w["alice"]).patch(url, data={"max_sources_per_run": 500}, content_type=J).status_code == 403


# ---- 5. one viewer scope -------------------------------------------------------------------------------------------

def test_the_outputs_list_never_mixes_organisations_unpinned(w):
    other_owner = user("zed")
    zeta = Workspace.objects.create(slug="zeta", display_name="Zeta", created_by=other_owner)
    WorkspaceMembership.objects.create(user=w["owner"], workspace=zeta, role=WorkspaceMembership.OWNER)
    assert _c(w["owner"]).get("/api/brain/outputs/").status_code == 422           # two trees: name one
    assert _c(w["owner"]).get("/api/w/acme/brain/outputs/").status_code == 200
