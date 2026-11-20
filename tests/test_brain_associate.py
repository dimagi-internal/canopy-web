"""Associations: the four layers, the port of canopy's `classify`, the precedence between them, and
the two things a person can rely on: a rejection sticks, and nothing sealed or foreign is ever linked."""
from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.brain import associate, sources
from apps.brain.models import BrainSource, ProjectSignature, SourceLink
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.harness import runner_requirements as rr
from apps.harness.models import Turn
from apps.workspaces.models import Workspace
from tests.fixtures.brain_world import brain_world, member, user

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = brain_world()
    lead = Agent.objects.create(slug="eva", name="Eva", owner=out["owner"], workspace=out["ws"])
    out["supply"] = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    out["sig"] = ProjectSignature.objects.create(
        project=out["supply"], repo="connect-labs", remote="dimagi-internal/connect-labs",
        paths=["connect_labs/supply_chain"], exclude_paths=["connect_labs/supply"],
        mcp_prefixes=["supply_chain_"], name_terms=["supply"], branch_patterns=["supply/*"],
        prs={"dimagi-internal/connect-labs#10": {"cross_cutting": False},
             "dimagi-internal/connect-labs#11": {"cross_cutting": False},
             "dimagi-internal/connect-labs#12": {"cross_cutting": True}})
    out["lead"] = lead
    return out


def session(w, *, title="", activity=None, **kw):
    s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, title=title,
                               activity=activity or {}, **kw)
    return s


def link_of(s, project):
    return SourceLink.objects.filter(source__ref=str(s.pk), subject=str(project.pk)).first()


def look(s):
    associate.associate(sources.for_session(s))
    return s


# ---- the port ------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,hit", [
    ("/Users/a/wt/connect_labs/supply_chain/models.py", True),
    ("connect_labs/supply_chain", True),
    ("supply_chain/stock", True),                    # activity records some dirs relative
    ("/Users/a/wt/connect_labs/supply/views.py", False),
    ("connect_labs/supply", False),                  # a sibling, not a prefix
    ("/Users/a/wt/other/supply_chain/x.py", False),  # an absolute path needs the whole spec
])
def test_path_matches_is_canopy_s(path, hit):
    assert associate.path_matches(path, ["connect_labs/supply_chain"]) is hit


def test_classify_tiers():
    c = associate.classify
    assert c(["owns 1"], [], owned=1, edits=0, mcp=0, excluded_edits=0, mcp_min=3)[0] == "adjacent"   # a lone PR
    assert c(["owns 1", "edits"], [], owned=1, edits=4, mcp=0, excluded_edits=0, mcp_min=3)[0] == "core"
    assert c(["calls"], [], owned=0, edits=0, mcp=5, excluded_edits=0, mcp_min=3)[0] == "core"
    assert c([], ["supply"], owned=0, edits=0, mcp=0, excluded_edits=0, mcp_min=3)[0] == "candidate"
    assert c([], ["supply"], owned=0, edits=0, mcp=0, excluded_edits=2, mcp_min=3)[0] == ""          # excluded only
    assert c([], [], owned=0, edits=0, mcp=0, excluded_edits=0, mcp_min=3) == ("", [])


# ---- layer 2: signals, against a hand-checked list ------------------------------------------------

EDIT = {"paths": {"connect-labs:connect_labs/supply_chain": 6}}
TRULY_SUPPLY = {
    "edits": EDIT,
    "owns a PR": {"prs": [{"repo": "dimagi-internal/connect-labs", "number": 10, "actions": ["created"]}],
                  **EDIT},
    "mcp heavy": {"mcp_tools": {"mcp__connect_labs__supply_chain_tender_list": 4}},
    "edits and mcp": {"paths": {"connect-labs:supply_chain/stock": 2},
                      "mcp_tools": {"mcp__connect_labs__supply_chain_item_get": 3}},
    "two PRs": {"prs": [{"repo": "dimagi-internal/connect-labs", "number": 10, "actions": ["created"]},
                        {"repo": "dimagi-internal/connect-labs", "number": 11, "actions": ["created"]}]},
    "edits plus a lone PR": {"prs": [{"repo": "dimagi-internal/connect-labs", "number": 11,
                                      "actions": ["created"]}], **EDIT},
    "edits in the project beside excluded ones": {"paths": {"connect-labs:connect_labs/supply_chain": 3,
                                                             "connect-labs:connect_labs/supply": 9}},
    "relative dir": {"paths": {"connect_labs/supply_chain/views": 5}},
    "mcp exactly three": {"mcp_tools": {"mcp__connect_labs__supply_chain_x": 3}},
    "edits two repos": {"paths": {"connect-labs:connect_labs/supply_chain": 1, "other:src": 40}},
}
NOT_SUPPLY = {
    "other repo with same dir": {"paths": {"other:connect_labs/supply_chain": 8}},
    "excluded edits only": {"paths": {"connect-labs:connect_labs/supply": 8}},
    "nothing": {},
    "generic mcp": {"mcp_tools": {"mcp__plugin_canopy_canopy-web__list_tasks": 30}},
    "two mcp calls": {"mcp_tools": {"mcp__connect_labs__supply_chain_x": 2}},
}


def test_core_precision_and_recall_against_the_hand_list(w):
    by_name = {}
    for name, act in {**TRULY_SUPPLY, **NOT_SUPPLY}.items():
        by_name[name] = look(session(w, title=f"s: {name}" if name in TRULY_SUPPLY else name, activity=act))
    truth = [by_name[n].pk for n in TRULY_SUPPLY]
    out = associate.compare(w["supply"], truth)
    assert out["precision"] >= 0.9 and out["recall"] >= 0.9, out
    assert out["extra"] == [] and out["missed"] == []
    # the weak-only and lone-PR sessions are not core
    assert link_of(by_name["nothing"], w["supply"]) is None
    assert link_of(by_name["excluded edits only"], w["supply"]) is None


def test_a_lone_owned_pr_is_adjacent_and_a_name_alone_is_a_candidate(w):
    lone = look(session(w, activity={"prs": [{"repo": "dimagi-internal/connect-labs", "number": 10,
                                              "actions": ["created"]}]}))
    named = look(session(w, title="supply tender view"))
    cross = look(session(w, activity={"prs": [{"repo": "dimagi-internal/connect-labs", "number": 12,
                                               "actions": ["created"]}]}))
    branch = look(session(w, activity={"branches": ["supply/tenders"]}))
    assert link_of(lone, w["supply"]).tier == "adjacent"
    assert link_of(named, w["supply"]).tier == "candidate"
    assert link_of(cross, w["supply"]).tier == "candidate"          # cross-cutting: weak
    assert link_of(branch, w["supply"]).tier == "candidate"
    assert "owns 1 project PR(s)" in link_of(lone, w["supply"]).evidence


def test_a_signals_link_goes_when_the_signal_does_and_a_stronger_one_never_does(w):
    s = look(session(w, activity=EDIT))
    assert link_of(s, w["supply"]).tier == "core"
    ProjectSignature.objects.filter(pk=w["sig"].pk).update(paths=["elsewhere/dir"])
    associate.associate(sources.for_session(s))
    assert link_of(s, w["supply"]) is None
    d = look(session(w, activity=EDIT))
    associate.put(sources.for_session(d), w["supply"], how="dispatched", tier="core", evidence=["task:1"])
    ProjectSignature.objects.filter(pk=w["sig"].pk).update(paths=["elsewhere/dir"])
    associate.associate(sources.for_session(d))
    assert link_of(d, w["supply"]).how == "dispatched"              # the signal went; the certain link stays


# ---- layer 1: dispatched -------------------------------------------------------------------------

def test_a_session_a_task_s_turn_ran_in_is_core_and_certain(w):
    task = AgentTask.objects.create(agent=w["lead"], ext_id="T1", title="x", project=w["supply"])
    s = session(w)
    Turn.objects.create(agent=w["lead"], origin=Turn.ORIGIN_API, prompt="p", idempotency_key=uuid.uuid4().hex,
                        raised_from_task=task, canopy_session=s)
    look(s)
    link = link_of(s, w["supply"])
    assert (link.tier, link.how, link.evidence) == ("core", "dispatched", [f"task:{task.pk}"])
    chat = session(w)
    Turn.objects.create(origin=Turn.ORIGIN_API, prompt="p", idempotency_key=uuid.uuid4().hex,
                        raised_from_task=task, chat_session=chat)
    assert link_of(look(chat), w["supply"]).how == "dispatched"


def test_a_personal_project_and_another_workspace_s_project_are_never_linked(w):
    personal = AgentProject.objects.create(agent=w["lead"], ext_id="P2", name="Mine", handle="mine",
                                           visibility=AgentProject.PERSONAL, owner_user=w["alice"])
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["owner"])
    stranger_lead = Agent.objects.create(slug="zed", name="Zed", owner=w["owner"], workspace=other)
    foreign = AgentProject.objects.create(agent=stranger_lead, ext_id="P1", name="Foreign", handle="foreign")
    ProjectSignature.objects.create(project=personal, paths=["connect_labs/supply_chain"], repo="connect-labs")
    ProjectSignature.objects.create(project=foreign, paths=["connect_labs/supply_chain"], repo="connect-labs")
    s = session(w, activity=EDIT)
    for p in (personal, foreign):
        t = AgentTask.objects.create(agent=w["lead"] if p is personal else stranger_lead, ext_id=f"T{p.pk}",
                                     title="x", project=p)
        Turn.objects.create(agent=w["lead"], origin=Turn.ORIGIN_API, prompt="p", idempotency_key=uuid.uuid4().hex,
                            raised_from_task=t, canopy_session=s)
    look(s)
    assert set(SourceLink.objects.filter(source__ref=str(s.pk)).values_list("subject", flat=True)) == {
        str(w["supply"].pk)}


def test_a_sealed_session_is_not_looked_at(w):
    s = session(w, activity=EDIT, metadata={rr.METADATA_KEY: ["sensitive-data"]})
    src = sources.for_session(s)
    assert associate.associate(src) is False
    assert not SourceLink.objects.exists()
    src.refresh_from_db()
    assert src.associated_at is not None and src.associate_due_at is None


# ---- precedence and the rejection that sticks ----------------------------------------------------

def test_declared_outranks_signals_and_a_persons_rejection_survives_every_rerun(w):
    s = look(session(w, activity=EDIT))
    src = sources.for_session(s)
    assert link_of(s, w["supply"]).how == "signals"
    row = associate.declare(src, w["supply"], tier="rejected", by=w["alice"])
    assert (row.tier, row.how, row.by_id) == ("rejected", "declared", w["alice"].pk)
    for _ in range(3):                                                  # a re-run, a re-seed, a dispatch
        associate.associate(src)
        associate.put(src, w["supply"], how="dispatched", tier="core")
        associate.put(src, w["supply"], how="judged", tier="core", confidence=0.99, model="m")
        associate.declare(src, w["supply"], tier="core", by_agent=w["lead"])
    assert (link_of(s, w["supply"]).tier, link_of(s, w["supply"]).by_id) == ("rejected", w["alice"].pk)
    assert associate.undeclare(src, w["supply"], by=w["alice"])
    associate.associate(src)
    assert (link_of(s, w["supply"]).tier, link_of(s, w["supply"]).how) == ("core", "signals")   # resumes


def test_the_layers_rank_in_order(w):
    s = session(w)
    src = sources.for_session(s)
    p = w["supply"]
    associate.put(src, p, how="signals", tier="candidate")
    associate.put(src, p, how="judged", tier="adjacent", confidence=0.7, model="m")
    assert link_of(s, p).how == "judged"
    associate.put(src, p, how="signals", tier="core")                  # weaker: ignored
    assert (link_of(s, p).how, link_of(s, p).tier) == ("judged", "adjacent")
    associate.put(src, p, how="dispatched", tier="core")
    associate.declare(src, p, tier="adjacent", by_agent=w["lead"])
    assert (link_of(s, p).how, link_of(s, p).tier) == ("declared", "adjacent")
    associate.put(src, p, how="dispatched", tier="core")               # the agent's call outranks it
    assert link_of(s, p).how == "declared"
    associate.declare(src, p, tier="core", by=w["alice"])              # a person outranks the agent
    associate.declare(src, p, tier="adjacent", by_agent=w["lead"])
    assert (link_of(s, p).by_id, link_of(s, p).tier) == (w["alice"].pk, "core")
    assert SourceLink.objects.count() == 1


def test_one_person_s_rejection_is_not_overturned_by_another(w):
    s = session(w)
    src = sources.for_session(s)
    associate.declare(src, w["supply"], tier="rejected", by=w["alice"])
    row = associate.declare(src, w["supply"], tier="core", by=w["bob"])
    assert (row.tier, row.by_id) == ("rejected", w["alice"].pk)


# ---- debounce and the worker ---------------------------------------------------------------------

def test_a_fold_marks_the_source_due_once_and_the_worker_looks_and_clears_it(w, django_capture_on_commit_callbacks):
    s = session(w, activity={})
    src = sources.for_session(s)
    associate.associate(src)
    src.refresh_from_db()
    assert src.associate_due_at is None
    with django_capture_on_commit_callbacks(execute=True):
        s.activity = EDIT
        s.save(update_fields=["activity", "activity_keys"])
    src.refresh_from_db()
    first = src.associate_due_at
    assert first is not None and first > timezone.now()
    with django_capture_on_commit_callbacks(execute=True):
        s.activity = {**EDIT, "branches": ["x"]}
        s.save(update_fields=["activity"])
    src.refresh_from_db()
    assert src.associate_due_at == first                                # a burst of folds is one look
    with django_capture_on_commit_callbacks(execute=True):
        s.save(update_fields=["title"])                                 # not a fold
    BrainSource.objects.filter(pk=src.pk).update(associate_due_at=timezone.now() - dt.timedelta(seconds=1))
    out = associate.run_due()
    assert out["looked"] >= 1
    assert link_of(s, w["supply"]).tier == "core"
    src.refresh_from_db()
    assert src.associate_due_at is None


def test_the_worker_makes_sources_for_sessions_that_have_none_and_a_second_pass_changes_nothing(w):
    s = session(w, activity=EDIT)
    assert not BrainSource.objects.filter(ref=str(s.pk)).exists()
    associate.run_due()
    assert link_of(s, w["supply"]).tier == "core"
    n = SourceLink.objects.count()
    associate.run_due()
    assert SourceLink.objects.count() == n


def test_the_worker_is_registered_as_a_tick():
    from apps.common import ticks

    assert "brain.associate" in ticks.jobs()


# ---- seeding a signature -------------------------------------------------------------------------

def test_a_signature_is_seeded_from_the_certain_sessions_and_only_widened(w):
    ProjectSignature.objects.filter(pk=w["sig"].pk).delete()
    for _ in range(3):
        s = session(w, activity={
            "repos": ["connect-labs"], "remotes": ["dimagi-internal/connect-labs"],
            "paths": {"connect-labs:connect_labs/supply_chain": 4, **({"connect-labs:docs/x": 1} if _ == 0 else {})},
            "mcp_tools": {"mcp__connect_labs__supply_chain_tender_list": 2,
                          "mcp__plugin_canopy_canopy-web__list_tasks": 9},
            "prs": [{"repo": "dimagi-internal/connect-labs", "number": 30 + _, "actions": ["created"]}]})
        associate.declare(sources.for_session(s), w["supply"], tier="core", by_agent=w["lead"])
    noise = session(w, activity={"paths": {"connect-labs:other/dir": 5}})          # not on the project
    sig = associate.seed_signature(w["supply"])
    assert sig.repo == "connect-labs" and sig.remote == "dimagi-internal/connect-labs"
    assert "connect_labs/supply_chain" in sig.paths and "other/dir" not in sig.paths
    assert "docs/x" not in sig.paths                                  # one session in three is not a signature
    assert sig.mcp_prefixes == ["supply_chain_"]                      # not the canopy server's tools
    assert "supply" in sig.name_terms and len(sig.prs) == 3
    sig.paths = [*sig.paths, "hand/added"]
    sig.save()
    again = associate.seed_signature(w["supply"])
    assert "hand/added" in again.paths                                # widened, never narrowed
    assert noise.pk and not SourceLink.objects.filter(source__ref=str(noise.pk)).exists()


def test_a_signals_link_never_feeds_the_signature(w):
    look(session(w, activity={"paths": {"connect-labs:new/dir": 5, "connect-labs:connect_labs/supply_chain": 5}}))
    assert "new/dir" not in associate.seed_signature(w["supply"]).paths


# ---- the routes ----------------------------------------------------------------------------------

def client(u):
    c = Client()
    c.force_login(u)
    return c


def url(s, handle="supply"):
    return f"/api/me/learning/sessions/{s.pk}/projects/{handle}/"


def put(c, u, body):
    return c.put(u, data=json.dumps(body), content_type="application/json")


def test_a_person_declares_rejects_and_takes_it_back(w):
    s = look(session(w, activity=EDIT))
    SessionParticipant.objects.create(session=s, user=w["bob"])
    c = client(w["alice"])
    r = put(c, url(s), {"tier": "rejected"})
    assert r.status_code == 200 and r.json()["projects"] == [
        {"project": "supply", "name": "Supply", "tier": "rejected", "how": "declared"}]
    assert client(w["bob"]).get(f"/api/me/learning/sessions/{s.pk}/").json()["projects"][0]["tier"] == "rejected"
    r = put(client(w["bob"]), url(s), {"tier": "core"})                # someone else's rejection is theirs to undo
    assert r.status_code == 409
    assert c.delete(url(s)).json()["projects"][0]["how"] == "signals"
    assert put(client(w["bob"]), url(s), {"tier": "core"}).json()["projects"][0]["how"] == "declared"


def test_the_routes_refuse_a_stranger_an_unreadable_project_and_a_bad_tier(w):
    s = session(w)
    stranger = user("stranger")
    member(stranger, w["ws"])
    assert put(client(stranger), url(s), {"tier": "core"}).status_code == 404
    assert put(client(w["alice"]), url(s, "nope"), {"tier": "core"}).status_code == 404
    AgentProject.objects.create(agent=w["lead"], ext_id="P9", name="Hers", handle="hers",
                                visibility=AgentProject.PERSONAL, owner_user=w["carol"])
    assert put(client(w["alice"]), url(s, "hers"), {"tier": "core"}).status_code == 404     # not hers to read
    assert put(client(w["alice"]), url(s), {"tier": "candidate"}).status_code == 422
    assert not SourceLink.objects.exists()
    other_ws = Workspace.objects.create(slug="o2", display_name="O2", created_by=w["owner"])
    lead2 = Agent.objects.create(slug="yan", name="Yan", owner=w["owner"], workspace=other_ws)
    AgentProject.objects.create(agent=lead2, ext_id="P1", name="X", handle="elsewhere")
    assert put(client(w["alice"]), url(s, "elsewhere"), {"tier": "core"}).status_code == 404   # another workspace


def test_the_session_view_hides_links_to_projects_the_reader_may_not_read(w):
    s = session(w)
    hers = AgentProject.objects.create(agent=w["lead"], ext_id="P9", name="Hers", handle="hers",
                                       visibility=AgentProject.PERSONAL, owner_user=w["carol"])
    associate.put(sources.for_session(s), hers, how="declared", tier="core")
    associate.put(sources.for_session(s), w["supply"], how="signals", tier="core")
    assert [p["project"] for p in client(w["alice"]).get(f"/api/me/learning/sessions/{s.pk}/").json()["projects"]
            ] == ["supply"]


def test_an_agent_login_outside_a_running_turn_cannot_declare(w):
    bot = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"], user=user("hal_login"))
    member(bot.user, w["ws"])
    s = session(w)
    r = put(client(bot.user), f"/api/brain/sessions/{s.pk}/projects/supply/", {"tier": "core"})
    assert r.status_code == 403
    assert put(client(w["alice"]), f"/api/brain/sessions/{s.pk}/projects/supply/", {"tier": "core"}).status_code == 403
    assert not SourceLink.objects.exists()


# ---- an agent at close-out -----------------------------------------------------------------------

def _running_turn(w, s):
    """A turn of an agent with its own login, running on a box, in session `s`, and that login's token."""
    from apps.agents.models import AgentAdmin
    from apps.harness import turn_agent_tokens
    from apps.harness.models import Runner, RunnerAssignment

    bot = Agent.objects.create(slug="echo", name="Echo", owner=w["owner"], workspace=w["ws"], user=user("echo_login"))
    member(bot.user, w["ws"])
    op = user("op")
    member(op, w["ws"])
    AgentAdmin.objects.create(agent=bot, user=op)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=op, workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=bot, runner=box, rank=0)
    turn = Turn.objects.create(agent=bot, origin=Turn.ORIGIN_EMAIL, prompt="x", idempotency_key=uuid.uuid4().hex,
                               status=Turn.CLAIMED, claimed_by=box, canopy_session=s)
    return bot, turn, turn_agent_tokens.issue(turn, box)["token"]


def test_an_agent_in_its_running_turn_declares_its_own_session_only(w):
    s, other = session(w), session(w)
    bot, turn, tok = _running_turn(w, s)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}

    def call(sess, handle="supply", tier="core"):
        return Client().put(f"/api/brain/sessions/{sess.pk}/projects/{handle}/", data=json.dumps({"tier": tier}),
                            content_type="application/json", **auth)

    r = call(s)
    assert r.status_code == 200, r.content
    link = link_of(s, w["supply"])
    assert (link.how, link.tier, link.by_agent_id, link.by_id) == ("declared", "core", bot.pk, None)
    assert call(other).status_code == 404                               # not the conversation this turn runs in
    assert call(s, "nope").status_code == 404
    assert call(s, tier="rejected").status_code == 422                  # only a person rejects
    associate.declare(sources.for_session(s), w["supply"], tier="rejected", by=w["alice"])
    assert call(s).status_code == 200
    assert link_of(s, w["supply"]).tier == "rejected"                   # and never over a person's call


def test_an_agent_declares_nothing_about_a_sealed_session_or_a_project_it_cannot_read(w):
    s = session(w, metadata={rr.METADATA_KEY: ["sensitive-data"]})
    bot, turn, tok = _running_turn(w, s)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}
    r = Client().put(f"/api/brain/sessions/{s.pk}/projects/supply/", data=json.dumps({"tier": "core"}),
                     content_type="application/json", **auth)
    assert r.status_code == 404 and not SourceLink.objects.exists()
