"""The quality of what learners write: every quote is grounded in a row the run read, a dismissed finding stays
dismissed through its cooldown, a done one that comes back says so, a finding knows how many sessions and people it
reaches, it fades by the window of its type, and each target has a scorecard of how its findings land."""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import budget, context, emit, findings, grounding, reading, runs, scorecard, sources, targets
from apps.brain.models import BrainOutput, BrainRunRead, BrainSource, LearningConsent, OutputLineage
from apps.canopy_sessions.models import Message, Session
from tests.fixtures.brain_world import aim, brain_world, member, user

pytestmark = pytest.mark.django_db
J = "application/json"


def _ago(**kw):
    return timezone.now() - dt.timedelta(**kw)


def _chat(ws, who, title, lines):
    s = Session.objects.create(workspace=ws, created_by=who, origin=Session.ORIGIN_WEB, title=title)
    for i, text in enumerate(lines):
        Message.objects.create(session=s, role=Message.USER, turn_index=i, plaintext=text, author={"user_id": who.pk})
    return sources.for_session(s)


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


LINES = ["Please deploy the labs branch now", "The deploy failed: “permission denied” on   the ECS role",
         "I'll retry it — with the admin role this time", "ok"]


def _run(w, *chats):
    for who, title in chats or (("alice", "deploy"),):
        _chat(w["ws"], w[who], title, LINES)
    run, why = targets.start(w["target"])
    assert run is not None, why
    page = reading.read_next(run, limit=5)
    return run, [s["source"] for s in page["sources"]]


def _emit(run, src, evidence, key="deploy:ecs-role", **body):
    return emit.emit(run, kind="finding", title=f"deploys fail on the ECS role ({key})", source_ids=[src], key=key,
                     body={"type": "provision", **body, "evidence": evidence})


# ---- 1. quote grounding --------------------------------------------------------------------------------------------

def test_fold_ignores_case_whitespace_quotes_dashes_and_marks():
    assert grounding.holds('THE DEPLOY failed: "permission denied"', LINES[1])
    assert grounding.holds("i'll retry it - with the **admin** role", LINES[2])
    assert grounding.holds("Please deploy … branch now", LINES[0])
    assert grounding.holds("Please deploy [...] now", LINES[0])
    assert not grounding.holds("branch now ... Please deploy", LINES[0])       # pieces must come in order
    assert not grounding.holds("Please deploy the main branch", LINES[0])


def test_a_verbatim_or_normalised_quote_of_a_row_the_run_read_is_kept(w):
    run, [src] = _run(w)
    out = _emit(run, src, [{"source": src, "index": 1, "quote": "the deploy failed: \"permission denied\" on the ECS role"},
                           {"source": src, "index": 2, "quote": "retry it … admin role"}])
    assert out.folded is False and len(out.body["evidence"]) == 2


def test_a_quote_not_in_its_row_is_refused_naming_the_item_and_why(w):
    run, [src] = _run(w)
    good = {"source": src, "index": 0, "quote": "Please deploy the labs branch"}
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [good, {"source": src, "index": 1, "quote": "the deploy timed out on the ECS role"}])
    assert "evidence[1]" in str(e.value) and f"source {src}" in str(e.value) and "message 1" in str(e.value)
    assert "not in that message" in str(e.value) and "permission denied" in str(e.value)    # what the row says
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [{"source": src, "index": 0, "quote": "with the admin role this time"}])
    assert "it is in message 2" in str(e.value)                                         # where it really is
    assert BrainOutput.objects.count() == 0


def test_evidence_from_a_session_the_run_did_not_read_or_past_where_it_read_is_refused(w):
    run, [src] = _run(w)
    other = _chat(w["ws"], w["bob"], "other", ["Please deploy the labs branch now"])
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [{"source": other.pk, "index": 0, "quote": "Please deploy the labs branch"}])
    assert "did not read" in str(e.value) and "evidence[0]" in str(e.value)
    BrainRunRead.objects.filter(run=run, source_id=src).update(through=1)
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [{"source": src, "index": 2, "quote": "retry it with the admin role"}])
    assert "only through message 1" in str(e.value)
    with pytest.raises(emit.EmitRefusedError):
        _emit(run, src, ["a quote without a source"])
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [{"source": src, "index": 3, "quote": "o"}])
    assert "only through" in str(e.value)


def test_a_missing_index_is_found_and_evidence_sources_are_cited(w):
    run, ids = _run(w, ("alice", "deploy"), ("bob", "again"))
    a, b = ids
    out = emit.emit(run, kind="finding", title="t", source_ids=[a], key="k",
                    body={"evidence": [{"source": b, "quote": "permission denied"}]})
    assert out.body["evidence"][0]["index"] == 1
    cited = set(OutputLineage.objects.filter(output_id=str(out.pk), cited=True).values_list("source_id", flat=True))
    assert cited == {a, b}


def test_a_quote_too_short_to_show_anything_is_refused_unless_it_is_the_whole_row(w):
    run, [src] = _run(w)
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src, [{"source": src, "index": 0, "quote": "deploy"}])
    assert "too short" in str(e.value)
    assert _emit(run, src, [{"source": src, "index": 3, "quote": "ok"}]).pk


# ---- 2. dismissals are remembered; a done one that comes back says so ---------------------------------------------

def _ev(src, i=1):
    return [{"source": src, "index": i, "quote": LINES[i]}]


def _again(w, title="again"):
    run, [src] = _run(w, ("bob", title))
    return run, src


def test_a_repeat_of_a_dismissed_finding_folds_and_stays_dismissed_until_the_cooldown_ends(w):
    run, [src] = _run(w)
    out = _emit(run, src, _ev(src))
    findings.set_status(out, "dismissed", by=w["owner"])
    runs.finish(run, ok=True)
    run, src = _again(w)
    doc = context.document(run)
    assert doc["dismissed_findings_total"] == 1 and doc["dismissed_findings"][0]["key"] == "deploy:ecs-role"
    assert "dismissed — don't re-raise unless something changed" in runs.prompt(w["sys"], run)
    again = _emit(run, src, _ev(src))
    assert again.folded and again.pk == out.pk and again.status == "dismissed" and again.seen == 2
    assert len(again.sightings) == 2
    runs.finish(run, ok=True)
    BrainOutput.objects.filter(pk=out.pk).update(status_at=_ago(days=budget.DISMISS_COOLDOWN_DAYS + 1))
    run, src = _again(w, "later")
    assert context.document(run)["dismissed_findings"] == []
    fresh = _emit(run, src, _ev(src))
    assert fresh.folded is False and fresh.pk != out.pk and fresh.status == "open" and fresh.recurs_of_id is None


def test_the_cooldown_is_the_target_s_to_set(w):
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/budget"
    r = _c(w["owner"]).patch(url, data={"dismiss_cooldown_days": 5}, content_type=J)
    assert r.status_code == 200 and r.json()["budget"]["dismiss_cooldown_days"] == 5
    w["target"].refresh_from_db()
    run, [src] = _run(w)
    out = _emit(run, src, _ev(src))
    findings.set_status(out, "dismissed", by=w["owner"])
    BrainOutput.objects.filter(pk=out.pk).update(status_at=_ago(days=6))
    assert _emit(run, src, _ev(src, 2)).pk != out.pk


def test_the_context_lists_dismissed_keys_newest_first_and_capped(w, monkeypatch):
    monkeypatch.setattr(findings, "MAX_DISMISSED_IN_CONTEXT", 2)
    run, [src] = _run(w)
    made = [_emit(run, src, _ev(src), key=f"k{i}") for i in range(3)]
    for i, o in enumerate(made):
        findings.set_status(o, "dismissed", by=w["owner"])
        BrainOutput.objects.filter(pk=o.pk).update(status_at=_ago(days=10 - i))
    total, rows = findings.dismissed_for(run, workspace=run.scope or None, limit=findings.MAX_DISMISSED_IN_CONTEXT)
    assert total == 3 and [r["key"] for r in rows] == ["k2", "k1"]
    shown = context.document(run)
    assert shown["dismissed_findings_total"] == 3 and len(shown["dismissed_findings"]) == 2


def test_a_repeat_of_a_done_finding_is_a_new_one_that_came_back(w):
    run, [src] = _run(w)
    out = _emit(run, src, _ev(src))
    findings.set_status(out, "done", by=w["owner"])
    runs.finish(run, ok=True)
    run, src = _again(w)
    back = _emit(run, src, _ev(src))
    assert back.folded is False and back.pk != out.pk and back.recurs_of_id == out.pk
    rows = {o["id"]: o for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/?status=open").json()}
    assert rows[back.pk]["came_back"] is True and rows[back.pk]["recurs_of"] == out.pk


def test_the_page_s_copy_of_a_run_context_names_no_dismissed_finding(w):
    run, [src] = _run(w)
    findings.set_status(_emit(run, src, _ev(src)), "dismissed", by=w["owner"])
    runs.finish(run, ok=True)
    run, _src = _again(w)
    runs.finish(run, ok=True)
    shown = next(r for r in _c(w["owner"]).get("/api/w/acme/brain/state/runs").json()["runs"] if r["id"] == run.pk)
    assert shown["context"]["dismissed_findings"] == [] and shown["context"]["dismissed_findings_total"] == 1


# ---- 3. breadth: sessions and people across every fold ------------------------------------------------------------

def test_a_finding_counts_the_sessions_and_people_it_reaches_across_folds(w):
    run, [src] = _run(w)
    out = _emit(run, src, _ev(src))
    row = next(o for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/").json() if o["id"] == out.pk)
    assert (row["sessions_seen"], row["people_seen"], row["breadth"]) == (1, 1, "single session")
    runs.finish(run, ok=True)
    run, src2 = _again(w)
    _emit(run, src2, _ev(src2))
    row = next(o for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/").json() if o["id"] == out.pk)
    assert (row["sessions_seen"], row["people_seen"], row["breadth"]) == (2, 2, "pattern")
    assert len(row["sightings"]) == 2 and row["sightings"] == sorted(row["sightings"])


def test_a_finding_made_before_sightings_were_kept_shows_when_it_was_made_and_last_seen(w):
    run, [src] = _run(w)
    out = _emit(run, src, _ev(src))
    BrainOutput.objects.filter(pk=out.pk).update(sightings=[], last_seen_at=_ago(days=1))
    out.refresh_from_db()
    assert len(findings.sighting_times(out)) == 2


# ---- 4. fading by the type of a finding ----------------------------------------------------------------------------

def test_a_finding_fades_by_the_window_of_its_type(w):
    run, [src] = _run(w)
    design = _emit(run, src, _ev(src), key="a", type="design")
    friction = _emit(run, src, _ev(src), key="b", type="defect", signal="friction")
    plain = _emit(run, src, _ev(src), key="c", type="something-else")
    BrainOutput.objects.filter(pk__in=[design.pk, friction.pk, plain.pk]).update(last_seen_at=_ago(days=30))
    faded = {o["id"]: o["faded"] for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/").json()}
    assert faded == {design.pk: False, friction.pk: True, plain.pk: True}
    assert {f["id"] for f in context.document(run)["open_findings"]} == {design.pk}
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/budget"
    r = _c(w["owner"]).patch(url, data={"fade_days": 40, "fade_days_by_type": {"design": 10}}, content_type=J)
    assert r.status_code == 200 and r.json()["budget"]["fade_windows"]["design"] == 10
    faded = {o["id"]: o["faded"] for o in _c(w["owner"]).get("/api/w/acme/brain/outputs/").json()}
    assert faded == {design.pk: True, friction.pk: True, plain.pk: False}      # no known type: the target's fade_days


def test_the_defaults_by_type_and_their_checks():
    assert budget.fade_days_for(None, {"type": "provision"}) == 21
    assert budget.fade_days_for(None, {"type": "skill"}) == budget.fade_days_for(None, {"signal": "over_claim"}) == 45
    assert budget.fade_days_for(None, {"type": "design", "signal": "friction"}) == 90       # its type first
    assert budget.fade_days_for(None, {}) == budget.FADE_DAYS and budget.fade_days_for(None, "x") == budget.FADE_DAYS


@pytest.mark.parametrize("bad", [{"design": 0}, {"design": 400}, {"no spaces": 10}, {"design": -3}])
def test_a_target_s_windows_by_type_are_checked(w, bad):
    url = f"/api/w/acme/brain/targets/{w['target'].pk}/budget"
    assert _c(w["owner"]).patch(url, data={"fade_days_by_type": bad}, content_type=J).status_code == 422


# ---- 5. the scorecard ----------------------------------------------------------------------------------------------

def test_the_scorecard_counts_each_target_s_findings_by_week_and_status(w):
    run, [src] = _run(w)
    made = [_emit(run, src, _ev(src), key=f"k{i}") for i in range(5)]
    for o, status in zip(made, ("accepted", "done", "dismissed", "dismissed"), strict=False):
        findings.set_status(o, status, by=w["owner"])
    BrainOutput.objects.filter(pk=made[4].pk).update(last_seen_at=_ago(days=60))         # open but faded
    runs.finish(run, ok=True)
    run, src = _again(w)
    _emit(run, src, _ev(src), key="k1")                                                   # came back after done
    got = _c(w["owner"]).get("/api/w/acme/brain/scorecard")
    assert got.status_code == 200, got.content
    body = got.json()
    assert len(body["weeks"]) == scorecard.WEEKS
    [t] = body["targets"]
    assert t["target"] == w["target"].pk and t["learner"] == "sys" and len(t["weeks"]) == scorecard.WEEKS
    assert (t["accepted"], t["done"], t["dismissed"], t["open"]) == (1, 1, 2, 2)
    assert t["precision"] == 0.5 and t["came_back"] == 1
    assert (t["open_now"], t["faded_now"]) == (2, 1)
    assert sum(wk["dismissed"] for wk in t["weeks"]) == 2 and t["weeks"][-1]["open"] == 2


def test_the_scorecard_counts_only_findings_the_viewer_may_read(w):
    run, [src] = _run(w)
    _emit(run, src, _ev(src))
    body = _c(w["alice"]).get("/api/w/acme/brain/scorecard").json()              # a member: system findings are not hers
    assert all(t["open"] == 0 and t["open_now"] == 0 and t["precision"] is None for t in body["targets"])


def test_week_starts_are_mondays_oldest_first():
    starts = scorecard.week_starts(dt.datetime(2026, 11, 19, 12, tzinfo=dt.UTC))
    assert starts[-1] == dt.date(2026, 11, 16) and starts[0] == dt.date(2026, 9, 28)
    assert all(d.weekday() == 0 for d in starts)


def test_a_finding_whose_source_was_a_session_without_rows_cannot_ground(w):
    s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, title="empty")
    src = sources.for_session(s)
    run, _ids = _run(w)
    BrainRunRead.objects.get_or_create(run=run, source=BrainSource.objects.get(pk=src.pk))
    with pytest.raises(emit.EmitRefusedError) as e:
        _emit(run, src.pk, [{"source": src.pk, "index": 0, "quote": "anything at all"}])
    assert "served no message of source" in str(e.value)
