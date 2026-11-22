"""Recency and repeats: a target's run reads only sessions active in its window, newest first, and a session that grew
since it was learned from is due again from where it stopped; a run is told the open findings its learner already holds
and folds a repeat into one (by key or `same_as`); two findings that are one problem are merged; an open finding no run
re-saw fades."""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import context, emit, findings, reading, runs, sources, targets
from apps.brain.models import BrainOutput, BrainRunRead, LearningConsent, OutputLineage
from apps.canopy_sessions.models import Message, Session
from apps.events.models import Event
from tests.fixtures.brain_world import aim, brain_world, member, user

pytestmark = pytest.mark.django_db
J = "application/json"


def _ago(**kw):
    return timezone.now() - dt.timedelta(**kw)


def _chat(ws, who, title, *, rows=1, age=None):
    s = Session.objects.create(workspace=ws, created_by=who, origin=Session.ORIGIN_WEB, title=title)
    for i in range(rows):
        Message.objects.create(session=s, role=Message.USER, turn_index=i, plaintext=f"{title} {i}",
                               author={"user_id": who.pk})
    if age is not None:
        Session.objects.filter(pk=s.pk).update(created_at=_ago(**age))
        Message.objects.filter(session=s).update(created_at=_ago(**age))
    sources.for_session(s)
    return s


def _grow(s, who, n, *, age=None):
    start = Message.objects.filter(session=s).count()
    for i in range(start, start + n):
        Message.objects.create(session=s, role=Message.USER, turn_index=i, plaintext=f"more {i}",
                               author={"user_id": who.pk})


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
    out["target"] = aim(sys, ws, by=owner)
    return out


def _start(w):
    run, why = targets.start(w["target"])
    assert run is not None, why
    return run


def _titles(page):
    return [s["title"] for s in page["sources"]]


# ---- the window, and newest first --------------------------------------------------------------------------------

def test_a_session_older_than_the_window_is_never_read_and_a_target_with_only_old_ones_does_not_fire(w):
    _chat(w["ws"], w["alice"], "old", age={"days": 20})
    assert targets.has_new(w["target"]) is False
    assert targets.start(w["target"]) == (None, "nothing new to learn from")
    _chat(w["ws"], w["alice"], "fresh", age={"days": 2})
    run = _start(w)
    page = reading.read_next(run, limit=5)
    assert _titles(page) == ["fresh"] and page["done"] is True
    assert context.document(run)["sessions_due"] == 0


def test_newest_activity_first_not_oldest_id(w):
    a = _chat(w["ws"], w["alice"], "a", age={"days": 5})
    _chat(w["ws"], w["bob"], "b", age={"days": 3})
    _chat(w["ws"], w["alice"], "c", age={"days": 9})
    Message.objects.create(session=a, role=Message.USER, turn_index=1, plaintext="a again",
                           author={"user_id": w["alice"].pk})           # the oldest session is the latest active
    run = _start(w)
    assert _titles(reading.read_next(run, limit=5)) == ["a", "b", "c"]


def test_the_window_is_the_target_s_and_its_managers_set_it(w):
    _chat(w["ws"], w["alice"], "old", age={"days": 20})
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/budget"
    r = _c(w["owner"]).patch(url, data={"lookback_days": 30, "fade_days": 10}, content_type=J)
    assert r.status_code == 200 and r.json()["budget"]["lookback_days"] == 30 and r.json()["budget"]["fade_days"] == 10
    assert _c(w["alice"]).patch(url, data={"lookback_days": 300}, content_type=J).status_code in (403, 404)
    w["target"].refresh_from_db()
    assert targets.has_new(w["target"]) is True
    assert context.document(_start(w))["lookback_days"] == 30


def test_a_project_target_reads_a_month_back_by_default():
    from apps.brain import budget
    from apps.brain.models import BrainTarget

    assert budget.lookback_days(BrainTarget(project_id=1)) == 30
    assert budget.lookback_days(BrainTarget()) == 14 and budget.lookback_days(None) is None


def test_counting_what_is_due_costs_the_same_however_many_sessions(w):
    for i in range(3):
        _chat(w["ws"], w["alice"], f"s{i}")
    run = _start(w)
    with CaptureQueriesContext(connection) as few:
        context.due(run)
    for i in range(12):
        _chat(w["ws"], w["bob"], f"t{i}")
    with CaptureQueriesContext(connection) as many:
        assert context.due(run)[0] == 15
    assert len(many) == len(few)


# ---- a session that grew is due again, from where it stopped ------------------------------------------------------

def test_a_session_that_grew_well_past_its_read_is_read_again_from_there(w):
    s = _chat(w["ws"], w["alice"], "long", rows=3)
    first = _start(w)
    reading.read_next(first, limit=5)
    assert BrainRunRead.objects.get(run=first).through == 2
    runs.finish(first, ok=True)
    _grow(s, w["alice"], 5)
    assert targets.has_new(w["target"]) is False                          # a little more is not worth a re-read
    _grow(s, w["alice"], reading.REREAD_ROWS)
    again = _start(w)
    [served] = reading.read_next(again, limit=5)["sources"]
    assert served["read_before"] == 2 and served["messages"][0]["index"] == 3
    assert BrainRunRead.objects.get(run=again).through == 2 + 5 + reading.REREAD_ROWS


# ---- the run is told the open findings, and folds a repeat ----------------------------------------------------------

def _quote(src, index=0):
    """A grounded piece of evidence: the words of row `index` of source `src`."""
    from apps.brain.models import BrainSource

    text = Message.objects.get(session=BrainSource.objects.get(pk=src).session, turn_index=index).plaintext
    return {"source": src, "index": index, "quote": text}


def _found(w, run, key="zsh:glob", title="zsh globs break", **kw):
    page = reading.read_next(run, limit=5)
    src = page["sources"][0]["source"] if page["sources"] else BrainRunRead.objects.filter(run=run).first().source_id
    return emit.emit(run, kind="finding", title=title, body={"what": title, "evidence": [_quote(src)]},
                     source_ids=[src], key=key, **kw)


def test_the_context_lists_open_findings_and_an_emit_with_an_existing_key_folds(w):
    _chat(w["ws"], w["alice"], "one")
    first = _start(w)
    out = _found(w, first)
    runs.finish(first, ok=True)
    _chat(w["ws"], w["bob"], "two")
    second = _start(w)
    doc = context.document(second)
    assert doc["open_findings_total"] == 1
    assert doc["open_findings"][0]["id"] == out.pk and doc["open_findings"][0]["key"] == "zsh:glob"
    lines = runs.prompt(w["sys"], second)
    assert f"#{out.pk} key `zsh:glob`: zsh globs break (seen 1" in lines and "reuse a key" in lines
    again = _found(w, second)
    assert again.folded is True and again.pk == out.pk and BrainOutput.objects.get(pk=out.pk).seen == 2
    # The page's copy of the context names no finding (who sees a run is not who sees its findings).
    shown = next(r for r in _c(w["owner"]).get("/api/w/acme/brain/state/runs").json()["runs"] if r["id"] == second.pk)
    assert shown["context"]["open_findings"] == [] and shown["context"]["open_findings_total"] == 1


def test_an_emit_can_name_the_finding_it_repeats(w):
    _chat(w["ws"], w["alice"], "one")
    run = _start(w)
    out = _found(w, run)
    same = _found(w, run, key="zsh:shell-traps", title="zsh traps on the mac runner", same_as=out.pk)
    assert same.folded is True and same.pk == out.pk
    with pytest.raises(emit.EmitRefusedError):
        _found(w, run, key="x", title="other", same_as=out.pk + 999)
    findings.set_status(out, "dismissed", by=w["owner"])          # lately dismissed: it takes the repeat, dismissed
    kept = _found(w, run, key="x", title="other again", same_as=out.pk)
    assert kept.folded is True and kept.pk == out.pk and kept.status == "dismissed"
    findings.set_status(out, "done", by=w["owner"])                # done: a repeat is news, never a fold
    with pytest.raises(emit.EmitRefusedError):
        _found(w, run, key="x", title="other once more", same_as=out.pk)


# ---- merge ---------------------------------------------------------------------------------------------------------

def test_merge_folds_a_duplicate_into_a_finding(w):
    _chat(w["ws"], w["alice"], "one")
    _chat(w["ws"], w["bob"], "two")
    run = _start(w)
    page = reading.read_next(run, limit=1)
    x = _quote(page["sources"][0]["source"])
    a = emit.emit(run, kind="finding", title="hands work back", body={"evidence": [x]},
                  source_ids=[page["sources"][0]["source"]], key="handback:a")
    page = reading.read_next(run, limit=1)
    y = _quote(page["sources"][0]["source"])
    b = emit.emit(run, kind="finding", title="asks before acting", body={"evidence": [y]},
                  source_ids=[page["sources"][0]["source"]], key="handback:b")
    url = f"/api/w/acme/brain/outputs/{a.pk}/merge"
    assert _c(w["alice"]).post(url, data={"duplicate": b.pk}, content_type=J).status_code == 404
    assert _c(w["owner"]).post(url, data={"duplicate": a.pk}, content_type=J).status_code == 422
    r = _c(w["owner"]).post(url, data={"duplicate": b.pk}, content_type=J)
    assert r.status_code == 200, r.content
    got = r.json()
    assert got["seen"] == 2 and got["body"]["evidence"] == [x, y] and got["body"]["merged_from"][0]["id"] == b.pk
    b.refresh_from_db()
    assert b.status == findings.DUPLICATE and b.merged_into_id == a.pk
    cited = set(OutputLineage.objects.filter(output_id=str(a.pk), cited=True).values_list("source_id", flat=True))
    assert len(cited) == 2
    assert Event.objects.filter(kind="output.merged").exists()
    listed = [o["id"] for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/").json()]
    assert a.pk in listed and b.pk not in listed
    # A repeat of the duplicate's key now folds into the finding it was merged into.
    again = emit.emit(run, kind="finding", title="asks before acting, again", body={},
                      source_ids=[page["sources"][0]["source"]], key="handback:b")
    assert again.folded is True and again.pk == a.pk
    assert _c(w["owner"]).post(f"/api/w/acme/brain/outputs/{b.pk}/status", data={"status": "open"},
                               content_type=J).status_code in (404, 422)


# ---- fading --------------------------------------------------------------------------------------------------------

def test_a_finding_nobody_re_sees_fades_and_can_be_kept(w):
    _chat(w["ws"], w["alice"], "one")
    run = _start(w)
    old = _found(w, run, key="old", title="old thing")
    new = _found(w, run, key="new", title="new thing")
    BrainOutput.objects.filter(pk=old.pk).update(last_seen_at=_ago(days=30))
    runs.finish(run, ok=True)
    rows = _c(w["owner"]).get("/api/w/acme/brain/outputs/").json()
    assert [(o["id"], o["faded"]) for o in rows] == [(new.pk, False), (old.pk, True)]
    _chat(w["ws"], w["bob"], "two")
    doc = context.document(_start(w))
    assert [f["id"] for f in doc["open_findings"]] == [new.pk]
    r = _c(w["owner"]).post(f"/api/w/acme/brain/outputs/{old.pk}/status", data={"status": "open"}, content_type=J)
    assert r.status_code == 200 and r.json()["faded"] is False
