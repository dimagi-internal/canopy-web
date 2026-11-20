"""Project learning: runs started on a schedule per active project and only for what is new, and the judge
that rules on weakly tied sessions (layer 3 of `associate`)."""
from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.brain import associate, consent, emit, reading, registry, runs, schedule, sources
from apps.brain.models import BrainCapability, BrainRun, LearningConsent, SourceLink
from apps.canopy_sessions.models import Message, Session
from apps.harness import claim_payload
from apps.harness.models import Runner, Turn
from apps.harness.signals import turn_status_changed
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from tests.fixtures.brain_world import brain_world, member, user

pytestmark = pytest.mark.django_db


def _chat(w, who, title, *lines):
    s = Session.objects.create(workspace=w["ws"], created_by=who, origin=Session.ORIGIN_WEB, title=title)
    for i, text in enumerate(lines):
        Message.objects.create(session=s, role=Message.USER, turn_index=i, plaintext=text, author={"user_id": who.pk})
    sources.for_session(s)
    return s


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = True
    settings.MCP_WRITE_LIMIT = 10_000
    out = brain_world()
    out.pop("session").delete()
    root = user("root")
    root.is_superuser = True
    root.save()
    member(root, out["ws"])
    op = Agent.objects.create(slug="eva", name="Eva", owner=out["owner"], workspace=out["ws"], user=user("eva_login"))
    member(op.user, out["ws"])
    out.update(root=root, operator=op)
    out["box"] = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=out["owner"], workspace=out["ws"],
                                       last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    out["project"] = AgentProject.objects.create(agent=op, ext_id="P1", name="Supply", handle="supply",
                                                 status=AgentProject.ACTIVE, outcome="Tenders run end to end")
    registry.update(out["proj"], operator=op, output_kinds=["idea"], enabled=True, schedule="daily", by=root)
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["project"])
    return out


def _link(w, s, project=None, tier="core"):
    associate.put(sources.for_session(s), project or w["project"], how="signals", tier=tier)


def _end(run, status=Turn.DONE):
    turn = Turn.objects.get(chat_session=run.session)
    Turn.objects.filter(pk=turn.pk).update(status=status, finished_at=timezone.now())
    turn.refresh_from_db()
    turn_status_changed.send(sender=Turn, turn=turn)
    run.refresh_from_db()
    return run


# ---- the schedule --------------------------------------------------------------------------------

def _titles(run):
    return [x["title"] for x in reading.read_next(run, limit=5)["sources"]]


def test_a_run_starts_per_active_project_only_when_there_is_something_new(w):
    assert schedule.run_due() == []                                       # nothing linked yet
    s = _chat(w, w["alice"], "tenders", "build the tender view")
    _link(w, s)
    [run] = schedule.run_due()
    assert (run.capability, run.subject, run.floor, run.purpose) == (w["proj"], str(w["project"].pk), 0, "learn")
    assert schedule.run_due() == []                                       # one is open; and not due yet
    reading.read_next(run, limit=5)
    _end(run)
    assert run.state == BrainRun.DONE
    assert schedule.run_due(now=timezone.now() + dt.timedelta(days=2)) == []          # due, but nothing new
    s2 = _chat(w, w["alice"], "more tenders", "add a filter")
    _link(w, s2)
    assert schedule.run_due() == []                                       # new, but not due yet (daily)
    [again] = schedule.run_due(now=timezone.now() + dt.timedelta(days=2))
    assert _titles(again) == ["more tenders"]                              # only what no clean run read


def test_a_session_linked_or_agreed_to_late_is_still_read(w):
    early = _chat(w, w["alice"], "early", "x")
    late = _chat(w, w["carol"], "late", "y")                            # carol has not agreed yet: not eligible
    _link(w, early)
    _link(w, late)
    newest = _chat(w, w["alice"], "newest", "z")
    _link(w, newest)
    [run] = schedule.run_due()
    assert _titles(run) == ["early", "newest"]
    _end(run)
    LearningConsent.objects.create(user=w["carol"], classes=["project"])  # late consent, an OLDER source id
    [again] = schedule.run_due(now=timezone.now() + dt.timedelta(days=2))
    assert _titles(again) == ["late"]                                     # a max-id cursor would have skipped it


def test_a_run_reads_past_its_floor_only(w):
    old, new = _chat(w, w["alice"], "old", "x"), _chat(w, w["alice"], "new", "y")
    _link(w, old)
    _link(w, new)
    run = runs.start(w["proj"], subject=w["project"], floor=sources.for_session(old).pk)
    page = reading.read_next(run, limit=5)
    assert [s["title"] for s in page["sources"]] == ["new"]


def test_a_failed_run_counts_nothing_and_is_tried_again(w):
    s = _chat(w, w["alice"], "tenders", "build the tender view")
    _link(w, s)
    [run] = schedule.run_due()
    reading.read_next(run, limit=5)
    _end(run, Turn.FAILED)
    assert run.state == BrainRun.FAILED
    [again] = schedule.run_due(now=timezone.now() + dt.timedelta(days=2))
    assert _titles(again) == ["tenders"]


def test_a_run_whose_turn_never_ends_is_abandoned_and_does_not_hold_the_project(w, settings):
    settings.BRAIN_RUN_TIMEOUT_SECONDS = 3600
    _link(w, _chat(w, w["alice"], "tenders", "build the tender view"))
    [stuck] = schedule.run_due()
    reading.read_next(stuck, limit=5)
    assert schedule.run_due(now=timezone.now() + dt.timedelta(minutes=30)) == []      # open, not yet stale
    [again] = schedule.run_due(now=timezone.now() + dt.timedelta(days=2))
    stuck.refresh_from_db()
    assert stuck.state == BrainRun.FAILED and _titles(again) == ["tenders"]
    with pytest.raises(reading.NotReadingError):
        reading.read_next(stuck)                                          # the abandoned run reads no more


def test_two_workers_cannot_open_the_same_run(w):
    _link(w, _chat(w, w["alice"], "tenders", "build the tender view"))
    runs.start(w["proj"], subject=w["project"])
    with pytest.raises(runs.StartRefusedError, match="already open"):
        runs.start(w["proj"], subject=w["project"])
    assert BrainRun.objects.filter(state=BrainRun.OPEN).count() == 1


def test_a_stale_output_s_remaining_sources_are_read_again_and_its_request_closed(w):
    from apps.brain import cascade
    from apps.brain.models import RederiveRequest

    a, b = _chat(w, w["alice"], "one", "we keep re-keying"), _chat(w, w["alice"], "two", "again the re-keying")
    _link(w, a)
    _link(w, b)
    [run] = schedule.run_due()
    ids = [x["source"] for x in reading.read_next(run, limit=5)["sources"]]
    both = emit.emit(run, kind="idea", title="Stop re-keying", body={"why": "y"}, source_ids=ids)
    _end(run)
    consent.exclude(w["alice"], sources.for_session(a))
    cascade.sweep()
    ask = RederiveRequest.objects.get(output=both, done_at__isnull=True)
    [again] = schedule.run_due(now=timezone.now() + dt.timedelta(days=2))
    page = reading.read_next(again, limit=5)
    assert [x["title"] for x in page["sources"]] == ["two"] and page["revoked"] == [ids[0]]
    assert reading.read_next(again, after=page["next_after"])["revoked"] == []           # told once
    _end(again)
    ask.refresh_from_db()
    assert ask.done_at is not None


@pytest.mark.parametrize("change", ["paused", "personal", "no_operator", "disabled", "no_schedule", "gate_off"])
def test_what_is_not_due_is_not_started(w, change, settings):
    _link(w, _chat(w, w["alice"], "tenders", "build the tender view"))
    if change == "paused":
        AgentProject.objects.filter(pk=w["project"].pk).update(status=AgentProject.PAUSED)
    elif change == "personal":
        AgentProject.objects.filter(pk=w["project"].pk).update(visibility=AgentProject.PERSONAL, owner_user=w["alice"])
    elif change == "no_operator":
        BrainCapability.objects.filter(pk=w["proj"].pk).update(operator=None)
    elif change == "disabled":
        registry.update(w["proj"], enabled=False)
    elif change == "no_schedule":
        registry.update(w["proj"], schedule="")
    else:
        settings.BRAIN_READS_ENABLED = False
    assert schedule.run_due() == []
    assert not BrainRun.objects.exists()


def test_two_projects_each_get_their_own_run_and_a_sessions_people_must_have_agreed(w):
    other = AgentProject.objects.create(agent=w["operator"], ext_id="P2", name="Other", handle="other",
                                        status=AgentProject.ACTIVE)
    a, b = _chat(w, w["alice"], "a", "x"), _chat(w, w["alice"], "b", "y")
    _link(w, a)
    _link(w, b, other)
    unagreed = _chat(w, w["carol"], "carol's", "z")                       # carol never agreed
    _link(w, unagreed)
    runs_ = schedule.run_due()
    assert {r.subject for r in runs_} == {str(w["project"].pk), str(other.pk)}
    page = reading.read_next(next(r for r in runs_ if r.subject == str(w["project"].pk)), limit=5)
    assert [s["title"] for s in page["sources"]] == ["a"]


def test_the_registry_validates_a_schedule(w):
    with pytest.raises(registry.RefusedError):
        registry.update(w["proj"], schedule="every minute")
    assert registry.update(w["proj"], schedule="weekly").schedule == "weekly"


def test_the_scheduler_is_a_tick():
    from apps.common import ticks

    assert "brain.schedule" in ticks.jobs()


# ---- the judge -----------------------------------------------------------------------------------

@contextlib.contextmanager
def as_token(raw):
    token = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    ctx = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(ctx)


def _call(name, args=None):
    return async_to_sync(mcp.call_tool)(name, args or {}).structured_content


def _judge(w):
    judge = registry.ensure_learners()["project-link-judge"]
    registry.update(judge, operator=w["operator"], enabled=True, by=w["root"])
    run = runs.start(BrainCapability.objects.get(pk=judge.pk), subject=w["project"])
    turn = Turn.objects.get(chat_session=run.session)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING, claimed_by=w["box"])
    turn.refresh_from_db()
    return run, claim_payload.issue_credentials(turn).mcp_token


def test_the_judge_reads_only_weak_candidates_short_with_the_projects_own_words(w):
    AgentTask.objects.create(agent=w["operator"], ext_id="T1", title="Fix the tender export", project=w["project"],
                             status="todo")
    weak = _chat(w, w["alice"], "supply thing", *[f"line {i} " + "word " * 200 for i in range(9)])
    core = _chat(w, w["alice"], "settled", "x")
    _link(w, weak, tier="candidate")
    _link(w, core, tier="core")
    run, raw = _judge(w)
    with as_token(raw):
        page = _call("brain_read", {"limit": 5})
    assert [s["title"] for s in page["sources"]] == ["supply thing"]          # not the settled one
    lines = page["sources"][0]["messages"]
    assert len(lines) == reading.JUDGE_LINES and all(len(m["text"]) <= reading.JUDGE_LINE_CHARS for m in lines)
    assert page["project"]["handle"] == "supply" and page["project"]["outcome"] == "Tenders run end to end"
    assert page["project"]["open_tasks"] == ["Fix the tender export"]


def test_a_judges_ruling_is_a_link_with_a_confidence_and_a_model_and_never_beats_a_person(w):
    weak = _chat(w, w["alice"], "supply thing", "tender view")
    refused = _chat(w, w["alice"], "supply-ish", "something else")
    _link(w, weak, tier="candidate")
    _link(w, refused, tier="candidate")
    associate.declare(sources.for_session(refused), w["project"], tier="rejected", by=w["alice"])
    run, raw = _judge(w)
    with as_token(raw):
        page = _call("brain_read", {"limit": 5})
        src = sources.for_session(weak).pk
        out = _call("brain_emit", {"kind": "link", "sources": [src], "body": {
            "tier": "core", "confidence": 0.82, "model": "haiku-5", "evidence": ["asks about the tender view"]}})
    link = SourceLink.objects.get(pk=out["id"])
    assert (link.tier, link.how, link.confidence, link.model) == ("core", "judged", 0.82, "haiku-5")
    assert link.evidence == ["asks about the tender view"]
    assert [s["title"] for s in page["sources"]] == ["supply thing"]            # a rejected one is not offered
    # the ruling is only for what was read, with the right shape
    from fastmcp.exceptions import ToolError

    for bad in ({"tier": "maybe", "confidence": 0.5, "model": "m"}, {"tier": "core", "confidence": 2, "model": "m"},
                {"tier": "core", "confidence": 0.5, "model": ""}, {"tier": "core", "confidence": True, "model": "m"}):
        with as_token(raw), pytest.raises(ToolError):
            _call("brain_emit", {"kind": "link", "sources": [src], "body": bad})
    with as_token(raw), pytest.raises(ToolError, match="did not read"):
        _call("brain_emit", {"kind": "link", "sources": [sources.for_session(refused).pk],
                             "body": {"tier": "core", "confidence": 0.9, "model": "m"}})
    assert SourceLink.objects.get(source=sources.for_session(refused)).tier == "rejected"


def test_the_judge_agrees_with_a_hand_list_through_the_pipeline(w):
    """The plumbing, with a rule standing in for the model: weak candidates in, rulings out, and `compare` measures
    them against a hand-checked list. The real figure (the Supply hand list, a model judge) is measured with
    `manage.py brain_validate_links` once a runner runs it."""
    belongs, elsewhere = [], []
    for i in range(10):
        s = _chat(w, w["alice"], f"supply item {i}", "work on the tender view" if i < 7 else "unrelated invoice")
        (belongs if i < 7 else elsewhere).append(s)
        _link(w, s, tier="candidate")
    run, raw = _judge(w)
    with as_token(raw):
        after = 0
        while True:
            page = _call("brain_read", {"limit": 5, "after": after})
            for src in page["sources"]:
                tier = "core" if any("tender" in m["text"] for m in src["messages"]) else "rejected"
                _call("brain_emit", {"kind": "link", "sources": [src["source"]],
                                     "body": {"tier": tier, "confidence": 0.9, "model": "rule"}})
            after = page["next_after"]
            if page["done"]:
                break
    out = associate.compare(w["project"], [s.pk for s in belongs], how="judged")
    assert out["precision"] >= 0.9 and out["recall"] >= 0.8, out
    assert sum(1 for s in elsewhere if SourceLink.objects.get(source=sources.for_session(s)).tier == "rejected") == 3


def test_an_idea_a_project_run_emits_carries_its_lineage(w):
    s = _chat(w, w["alice"], "tenders", "we keep re-keying the tender prices")
    _link(w, s)
    [run] = schedule.run_due()
    page = reading.read_next(run, limit=5)
    out = emit.emit(run, kind="idea", title="Import tender prices", body={"why": "re-keying"},
                    source_ids=[x["source"] for x in page["sources"]])
    assert out.subject == str(w["project"].pk)
    from apps.brain.models import OutputLineage

    assert OutputLineage.objects.get(output_id=str(out.pk)).source_id == sources.for_session(s).pk


# ---- which sessions a project is made of ---------------------------------------------------------

def _list(c, tiers=None):
    url = "/api/brain/workspaces/acme/projects/supply/sessions/" + (f"?tiers={tiers}" if tiers else "")
    return c.get(url)


def _client(u):
    from django.test import Client

    c = Client()
    c.force_login(u)
    return c


def test_the_project_s_sessions_are_listed_only_as_far_as_the_caller_may_read_them(w, settings):
    mine = _chat(w, w["alice"], "tenders", "build it")
    weak = _chat(w, w["alice"], "maybe", "hm")
    theirs = _chat(w, w["carol"], "carol's", "z")
    _link(w, mine)
    _link(w, weak, tier="candidate")
    _link(w, theirs)
    got = _list(_client(w["alice"])).json()
    assert [r["session_id"] for r in got] == [str(mine.pk)]                     # candidates are not in the default
    assert got[0]["tier"] == "core" and got[0]["how"] == "signals"
    assert {r["session_id"] for r in _list(_client(w["alice"]), "core,candidate").json()} == {str(mine.pk), str(weak.pk)}
    # carol reads alice's session through the project leg, once everyone in it agreed (alice did); never the gate off
    assert {r["session_id"] for r in _list(_client(w["carol"])).json()} == {str(theirs.pk), str(mine.pk)}
    settings.BRAIN_READS_ENABLED = False
    assert [r["session_id"] for r in _list(_client(w["carol"])).json()] == [str(theirs.pk)]
    # nothing but ids, titles and the way the tie was made, and where it was read from
    assert set(got[0]) == {"session_id", "title", "workspace", "tier", "how", "confidence", "started_at", "source"}
    assert got[0]["source"]["kind"] == "live"


def test_a_project_the_caller_may_not_read_is_not_found(w):
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility=AgentProject.PERSONAL, owner_user=w["alice"])
    assert _list(_client(w["bob"])).status_code == 404
    assert _list(_client(w["alice"])).status_code == 200
    assert _client(w["bob"]).get("/api/brain/workspaces/acme/projects/nope/sessions/").status_code == 404


# ---- a removal reaches what a project run learned -------------------------------------------------

def test_an_idea_withdraws_with_its_only_source_and_goes_stale_with_one_of_several(w):
    from apps.brain import cascade
    from apps.brain.models import BrainOutput

    a, b = _chat(w, w["alice"], "one", "we keep re-keying"), _chat(w, w["alice"], "two", "again the re-keying")
    _link(w, a)
    _link(w, b)
    [run] = schedule.run_due()
    page = reading.read_next(run, limit=5)
    ids = [x["source"] for x in page["sources"]]
    only = emit.emit(run, kind="idea", title="Import prices", body={"why": "x"}, source_ids=[ids[0]])
    both = emit.emit(run, kind="idea", title="Stop re-keying", body={"why": "y"}, source_ids=ids)
    consent.exclude(w["alice"], sources.for_session(a))
    cascade.sweep()
    only.refresh_from_db()
    both.refresh_from_db()
    assert only.state == BrainOutput.WITHDRAWN and only.body == {} and only.title == ""
    assert both.state == BrainOutput.STALE and both.body == {}                     # words gone, re-derivation queued
    from apps.brain.models import RederiveRequest

    assert RederiveRequest.objects.filter(output=both, done_at__isnull=True).exists()
    cascade.sweep()                                                                # idempotent
    consent.include(w["alice"], sources.for_session(a))
    cascade.sweep()
    only.refresh_from_db()
    assert only.state == BrainOutput.WITHDRAWN                                    # letting the session back in revives nothing
