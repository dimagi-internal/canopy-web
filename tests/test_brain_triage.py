"""Triage: a run spends its read budget on the sessions most worth reading. Each due session gets a cheap, explainable
verdict computed on the server from what the learner could read (friction in it, how much is new, how many people,
whether a person drove it), cached per (session, view, where the read starts, where it ends); the run is served and
told its sessions in that order, each with a one-line reason. Triage orders, never filters: every due session is
still due and `brain_read` can read any of them by id. Eligibility is untouched."""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import context, reading, runs, sources, targets, triage
from apps.brain.models import BrainRunRead, BrainTriage, LearningConsent
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from tests.fixtures.brain_world import aim, brain_world, member, user

pytestmark = pytest.mark.django_db


def _ago(**kw):
    return timezone.now() - dt.timedelta(**kw)


def _chat(ws, who, title, rows=(), *, age=None, others=()):
    """A session of `rows`: (role, text) or (role, text, extra content). USER rows are a person's (`typed`)."""
    s = Session.objects.create(workspace=ws, created_by=who, origin=Session.ORIGIN_WEB, title=title)
    for u in others:
        SessionParticipant.objects.create(session=s, user=u)
    _add(s, who, rows or [("user", f"{title} please")])
    if age is not None:
        Session.objects.filter(pk=s.pk).update(created_at=_ago(**age))
        Message.objects.filter(session=s).update(created_at=_ago(**age))
    sources.for_session(s)
    return s


def _add(s, who, rows):
    start = Message.objects.filter(session=s).count()
    for i, row in enumerate(rows, start):
        role, text, *extra = row
        Message.objects.create(session=s, role=role, turn_index=i, plaintext=text,
                               content=(extra[0] if extra else {}), typed=(role == Message.USER),
                               author=({"user_id": who.pk} if role == Message.USER else None))


def _err(text="boom"):
    return (Message.TOOL_RESULT, text, {"tool_use_id": "t", "is_error": True})


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
    out["target"] = aim(sys, ws, by=owner)
    return out


def _start(w):
    run, why = targets.start(w["target"])
    assert run is not None, why
    return run


def _titles(page):
    return [s["title"] for s in page["sources"]]


# ---- the verdict ---------------------------------------------------------------------------------------------------

def test_friction_ranks_above_recency_and_quiet_sessions_keep_newest_first(w):
    ws, alice, bob = w["ws"], w["alice"], w["bob"]
    _chat(ws, alice, "quiet new", age={"hours": 1})
    _chat(ws, alice, "quiet older", age={"days": 1})
    _chat(ws, alice, "rough", [("user", "deploy it"), ("assistant", "done"),
                               ("user", "no, that's wrong, it is still failing"), _err(), _err(),
                               ("user", "[Request interrupted by user for tool use]")], age={"days": 3})
    _chat(ws, bob, "denied", [("user", "clean up"), (Message.TOOL_RESULT,
                              "Permission to use Bash has been denied.", {"tool_use_id": "x", "is_error": True})],
          age={"days": 2})
    run = _start(w)
    # Before triage the order was newest activity first: quiet new, quiet older, denied, rough.
    assert _titles(reading.read_next(run, limit=5)) == ["rough", "denied", "quiet new", "quiet older"]


def test_the_verdict_is_explainable_and_names_no_words(w):
    s = _chat(w["ws"], w["alice"], "rough", [("user", "SECRET-PLAN go"), ("user", "no, stop, wrong file"),
                                             _err("SECRET-ERR"), _err()])
    run = _start(w)
    [v] = reading.verdicts(run)
    assert v.source == sources.for_session(s).pk
    assert v.signals["corrections"] == 1 and v.signals["tool_errors"] == 2 and v.signals["typed"] == 2
    assert v.score == triage.score(v.signals) > 0
    assert "1 correction" in v.reason and "2 tool errors" in v.reason and "person-driven" in v.reason
    assert "SECRET" not in v.reason and "SECRET" not in str(v.signals)


def test_an_agent_run_session_says_so_and_growth_counts(w):
    s = _chat(w["ws"], w["alice"], "bot", [("assistant", "working")] * 120)
    run = _start(w)
    [v] = reading.verdicts(run)
    assert v.signals["typed"] == 0 and v.signals["rows"] == 120 and "agent-run" in v.reason
    assert v.score == triage.score(v.signals) and v.score >= 2                        # 120 new rows is worth something
    assert s is not None


def test_more_people_rank_higher(w):
    _chat(w["ws"], w["alice"], "solo", age={"hours": 1})
    _chat(w["ws"], w["alice"], "pair", age={"days": 1}, others=[w["bob"]])
    run = _start(w)
    assert _titles(reading.read_next(run, limit=5)) == ["pair", "solo"]


def test_a_narrowed_scope_never_scores_what_it_cannot_read(w):
    w["sys"].scope = "conversation"
    w["sys"].save()
    _chat(w["ws"], w["alice"], "tools", [("user", "go"), _err(), _err(), _err()], age={"hours": 1})
    run = _start(w)
    [v] = reading.verdicts(run)
    assert v.view == "conversation" and v.signals["tool_errors"] == 0 and v.signals["rows"] == 1


# ---- re-reads: only what is new counts ------------------------------------------------------------------------------

def test_a_grown_session_is_scored_on_its_new_rows_only(w):
    s = _chat(w["ws"], w["alice"], "long", [("user", "no, wrong"), _err(), _err()])
    first = _start(w)
    reading.read_next(first, limit=5)
    runs.finish(first, ok=True)
    _add(s, w["alice"], [("assistant", "ok")] * reading.REREAD_ROWS)
    again = _start(w)
    [v] = reading.verdicts(again)
    assert v.after == 2 and v.signals["tool_errors"] == 0 and v.signals["corrections"] == 0
    assert v.signals["rows"] == reading.REREAD_ROWS


# ---- the cache -------------------------------------------------------------------------------------------------------

def test_a_verdict_is_cached_per_session_view_and_range_and_recomputed_when_the_session_grows(w):
    s = _chat(w["ws"], w["alice"], "one", [("user", "go"), _err()])
    run = _start(w)
    reading.verdicts(run)
    assert BrainTriage.objects.count() == 1
    with CaptureQueriesContext(connection) as again:
        reading.verdicts(run)
    assert BrainTriage.objects.count() == 1
    assert not any("INSERT" in q["sql"] for q in again.captured_queries)
    _add(s, w["alice"], [_err()])
    [v] = reading.verdicts(run)
    assert v.signals["tool_errors"] == 2 and BrainTriage.objects.count() == 2


def test_triage_costs_the_same_however_many_sessions(w):
    for i in range(3):
        _chat(w["ws"], w["alice"], f"s{i}", [("user", "go"), _err()])
    run = _start(w)                                   # starting it judged these three already: judge them cold
    BrainTriage.objects.all().delete()
    with CaptureQueriesContext(connection) as few:
        reading.verdicts(run)
    for i in range(12):
        _chat(w["ws"], w["bob"], f"t{i}", [("user", "go"), _err()])
    with CaptureQueriesContext(connection) as many:
        assert len(reading.verdicts(run)) == 15
    assert len(many) == len(few)


# ---- ordering, not filtering -----------------------------------------------------------------------------------------

def test_triage_never_drops_a_due_session_and_never_widens_what_is_due(w):
    _chat(w["ws"], w["alice"], "a")
    _chat(w["ws"], w["alice"], "b", [("user", "no, wrong"), _err()])
    _chat(w["ws"], w["carol"], "carol never agreed", [("user", "no!"), _err(), _err(), _err()])
    _chat(w["ws"], w["alice"], "old", [("user", "no"), _err()], age={"days": 30})
    run = _start(w)
    due = set(reading.candidates(run).values_list("pk", flat=True))
    assert {v.source for v in reading.verdicts(run)} == due and len(due) == 2
    assert _titles(reading.read_next(run, limit=5)) == ["b", "a"]


def test_brain_read_can_read_any_due_session_by_id_and_nothing_else(w):
    _chat(w["ws"], w["alice"], "rough", [("user", "no, wrong"), _err()])
    quiet = sources.for_session(_chat(w["ws"], w["alice"], "quiet"))
    carol = sources.for_session(_chat(w["ws"], w["carol"], "carol's"))
    run = _start(w)
    page = reading.read_next(run, source=quiet.pk)
    assert _titles(page) == ["quiet"] and page["done"] is False
    with pytest.raises(reading.NotDueError):
        reading.read_next(run, source=carol.pk)
    with pytest.raises(reading.NotDueError):
        reading.read_next(run, source=quiet.pk)                                   # read already: no longer due
    assert _titles(reading.read_next(run, limit=5)) == ["rough"]


def test_a_served_source_carries_its_verdict_and_the_read_keeps_it(w):
    s = _chat(w["ws"], w["alice"], "rough", [("user", "no, wrong"), _err()])
    run = _start(w)
    [served] = reading.read_next(run, limit=5)["sources"]
    assert served["triage"]["score"] > 0 and "correction" in served["triage"]["reason"]
    row = BrainRunRead.objects.get(run=run, source=sources.for_session(s))
    assert row.triage_score == served["triage"]["score"] and row.triage_reason == served["triage"]["reason"]


# ---- the run is told the order ---------------------------------------------------------------------------------------

def test_the_context_lists_the_due_sessions_in_triage_order_with_a_reason_each(w):
    _chat(w["ws"], w["alice"], "quiet", age={"hours": 1})
    rough = sources.for_session(_chat(w["ws"], w["alice"], "rough", [("user", "no, wrong"), _err()], age={"days": 2}))
    run = _start(w)
    doc = context.document(run)
    assert doc["source_ids_due"][0] == rough.pk and doc["sessions_due"] == 2
    assert doc["triage"][0]["source"] == rough.pk and "correction" in doc["triage"][0]["reason"]
    lines = runs.prompt(w["sys"], run)
    assert "most worth reading first" in lines and f"- source {rough.pk} (score {doc['triage'][0]['score']}): " in lines


def test_the_page_shows_a_read_s_verdict_and_never_the_ids_due(w):
    LearningConsent.objects.create(user=w["owner"], classes=["system"])
    _chat(w["ws"], w["owner"], "rough", [("user", "no, wrong"), _err()])
    run = _start(w)
    reading.read_next(run, limit=5)
    c = Client()
    c.force_login(w["owner"])                                  # their own session: they read it
    shown = next(r for r in c.get("/api/w/acme/brain/state/runs").json()["runs"] if r["id"] == run.pk)
    assert shown["context"]["triage"] == [] and shown["context"]["source_ids_due"] == []
    [read] = shown["sessions"]
    assert read["triage_score"] > 0 and "correction" in read["triage_reason"]
