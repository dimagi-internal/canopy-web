"""The brain's project leg (P4): who reads a project's sessions, how (read-only, scrubbed, recorded), and
the SQL form of `eligible` (`eligible_sources`) held to the per-source one over a whole world."""
from __future__ import annotations

import itertools
import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.brain import associate, consent, sources
from apps.brain.eligibility import eligible, eligible_sources
from apps.brain.models import BrainCapability, BrainSource, LearningConsent, SourceParticipant
from apps.canopy_sessions import access, audience
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.events.models import Event
from apps.harness import runner_requirements as rr
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.brain_world import brain_world, capability, member, user

pytestmark = pytest.mark.django_db

SECRET = "ghp_" + "0123456789abcdefABCDEF" + "0123456789abcd"          # joined, so the secret scan sees no token


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = True
    out = brain_world()
    lead = Agent.objects.create(slug="eva", name="Eva", owner=out["owner"], workspace=out["ws"])
    out["project"] = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    out["lead"] = lead
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["project"])
    out["reader"] = user("reader")
    member(out["reader"], out["ws"])
    Message.objects.create(session=out["session"], turn_index=0, role="user", plaintext=f"use {SECRET} please",
                           content={"text": f"use {SECRET} please"})
    Session.objects.filter(pk=out["session"].pk).update(title=f"deploy with {SECRET}")
    associate.put(out["source"], out["project"], how="dispatched", tier="core")
    return out


def client(u):
    c = Client()
    c.force_login(u)
    return c


def body(c, s):
    return c.get(f"/api/canopy-sessions/{s.pk}").json()


# ---- who reads, and how --------------------------------------------------------------------------

def test_a_projects_reader_reads_a_session_everyone_in_it_agreed_to_and_the_gate_is_off_by_default(w, settings):
    s = w["session"]
    assert access.can_read(w["reader"], s) is True
    assert access.role_for(w["reader"], s) == "viewer"
    assert not access.can_write(w["reader"], s) and not access.can_share(w["reader"], s)
    assert not access.may_pull(w["reader"], s) and not access.may_move(w["reader"], s)
    settings.BRAIN_READS_ENABLED = False
    assert access.can_read(w["reader"], s) is False
    assert client(w["reader"]).get(f"/api/canopy-sessions/{s.pk}").status_code == 404


def test_it_is_scrubbed_for_the_leg_and_not_for_the_people_who_were_in_it(w):
    s = w["session"]
    seen = body(client(w["reader"]), s)
    assert SECRET not in json.dumps(seen) and "redacted:github-token" in json.dumps(seen)
    assert SECRET not in seen["title"]
    for person in (w["alice"], w["bob"]):                    # the people in it read their own words
        assert SECRET in json.dumps(body(client(person), s))
    page = client(w["reader"]).get(f"/api/canopy-sessions/{s.pk}/messages?before=5").json()
    assert SECRET not in json.dumps(page)
    listed = client(w["reader"]).get("/api/canopy-sessions/?state=all").json()
    assert str(s.pk) in {r["id"] for r in listed} and SECRET not in json.dumps(listed)


def test_a_read_through_the_leg_is_recorded_once_per_reader_and_session_with_ids_only(w):
    client(w["reader"]).get(f"/api/canopy-sessions/{w['session'].pk}")
    client(w["reader"]).get(f"/api/canopy-sessions/{w['session'].pk}")
    rows = Event.objects.filter(source="brain.read", workspace_id="acme")
    assert rows.count() == 1 and rows.get().count == 2
    p = rows.get().payload
    assert p["capability"] == "project-readers" and p["reader_id"] == w["reader"].pk
    assert p["sources"] == [w["source"].pk] and SECRET not in json.dumps(p)
    client(w["alice"]).get(f"/api/canopy-sessions/{w['session'].pk}")               # her own words: no brain read
    assert Event.objects.filter(source="brain.read").count() == 1


@pytest.mark.parametrize("change,why", [
    (lambda w: consent.revoke(w["bob"]), "a participant revoked"),
    (lambda w: LearningConsent.objects.filter(user=w["bob"]).update(classes=["individual"]), "no project class"),
    (lambda w: consent.exclude(w["bob"], w["source"]), "a participant took the session out"),
    (lambda w: Session.objects.filter(pk=w["session"].pk).update(metadata={rr.METADATA_KEY: ["sensitive-data"]}),
     "sensitive"),
    (lambda w: Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now()), "words deleted"),
    (lambda w: SourceParticipant.objects.create(source=w["source"], contact=__import__(
        "apps.contacts.models", fromlist=["Contact"]).Contact.objects.create(workspace=w["ws"], email="c@x.org")),
     "a contact in it"),
    (lambda w: w["source"].__class__.objects.filter(pk=w["source"].pk).update(state=BrainSource.WITHDRAWN),
     "withdrawn"),
    (lambda w: associate.declare(w["source"], w["project"], tier="rejected", by=w["alice"]), "rejected by a person"),
    (lambda w: LearningConsent.objects.filter(user=w["bob"]).update(
        standing_rules=[{"class": "project"}]), "a standing rule"),
    (lambda w: LearningConsent.objects.filter(user=w["alice"]).update(workspaces=["elsewhere"], include_new=False),
     "outside the consent's workspaces"),
    (lambda w: WorkspaceMembership.objects.filter(user=w["bob"]).delete(), "a participant left the workspace"),
])
def test_anything_that_makes_it_ineligible_closes_the_leg_at_the_next_read(w, change, why):
    assert access.can_read(w["reader"], w["session"]) is True
    change(w)
    assert access.can_read(w["reader"], w["session"]) is False, why
    assert client(w["reader"]).get(f"/api/canopy-sessions/{w['session'].pk}").status_code == 404, why


def test_only_a_core_link_to_a_project_the_reader_may_read_opens_it(w):
    s = w["session"]
    associate.put(w["source"], w["project"], how="dispatched", tier="core")
    SourceLinkQ = type(w["source"].links.first())
    SourceLinkQ.objects.filter(source=w["source"]).update(tier="adjacent")
    assert not access.can_read(w["reader"], s)
    SourceLinkQ.objects.filter(source=w["source"]).update(tier="candidate")
    assert not access.can_read(w["reader"], s)
    SourceLinkQ.objects.filter(source=w["source"]).update(tier="core")
    assert access.can_read(w["reader"], s)
    stranger = user("stranger")
    assert not access.can_read(stranger, s)                        # not in the workspace: the tenant gate first


def test_a_personal_project_is_read_only_by_its_own_readers(w):
    mine = AgentProject.objects.create(agent=w["lead"], ext_id="P2", name="Mine", handle="mine",
                                       visibility=AgentProject.PERSONAL, owner_user=w["carol"])
    SourceLinkQ = type(w["source"].links.first())
    SourceLinkQ.objects.filter(source=w["source"]).delete()
    associate.put(w["source"], mine, how="declared", tier="core", by_agent=w["lead"])
    assert access.can_read(w["carol"], w["session"]) is True       # its lead person
    assert access.can_read(w["reader"], w["session"]) is False
    assert access.can_read(w["owner"], w["session"]) is False      # not even a workspace owner


def test_the_audience_names_the_leg_and_equals_the_acl_for_everyone(w):
    s = w["session"]
    people = [w["owner"], w["alice"], w["bob"], w["carol"], w["reader"], user("stranger")]
    for p in people:
        assert (p.pk in audience.readers(s)) is access.can_read(p, s), p.username
    assert w["reader"].pk in audience.readers(s)
    assert any("project" in x for x in audience.summary(s, w["alice"])["extras"])


def test_a_viewer_through_the_leg_cannot_act(w):
    s = w["session"]
    c = client(w["reader"])
    assert c.post(f"/api/canopy-sessions/{s.pk}/send", data=json.dumps({"text": "hi"}),
                  content_type="application/json").status_code in (403, 404)
    assert c.post(f"/api/canopy-sessions/{s.pk}/archive").status_code in (403, 404, 405)
    assert not SessionParticipant.objects.filter(session=s, user=w["reader"]).exists()


# ---- eligible_sources == eligible, over a world --------------------------------------------------

def _world(w):
    """Many sources: every combination of who is in it, who agreed, and what was done to it."""
    out = []
    people = [w["alice"], w["bob"], w["carol"]]
    for n, members in enumerate(itertools.chain.from_iterable(itertools.combinations(people, k) for k in (1, 2, 3))):
        s = Session.objects.create(workspace=w["ws"], created_by=members[0], origin=Session.ORIGIN_WEB,
                                   title=str(n))
        for u in members[1:]:
            SessionParticipant.objects.create(session=s, user=u)
        out.append(sources.for_session(s))
    return out


@pytest.mark.parametrize("scenario", ["nobody", "alice", "alice+bob", "all", "alice+bob rule", "carol restricted",
                                      "bob excluded", "carol left"])
@pytest.mark.parametrize("cap,purpose,subject", [("proj", "learn", None), ("sys", "learn", None),
                                                 ("coach", "coach", "alice"), ("mem", "remember", "bob")])
def test_the_one_query_agrees_with_the_one_question(w, scenario, cap, purpose, subject):
    LearningConsent.objects.all().delete()
    classes = {"proj": "project", "sys": "system", "coach": "individual", "mem": "memory"}
    every = ["project", "system", "individual", "memory"]
    cover = {"nobody": [], "alice": ["alice"], "alice+bob": ["alice", "bob"], "all": ["alice", "bob", "carol"],
             "alice+bob rule": ["alice", "bob"], "carol restricted": ["alice", "bob", "carol"],
             "bob excluded": ["alice", "bob", "carol"], "carol left": ["alice", "bob", "carol"]}[scenario]
    for name in cover:
        LearningConsent.objects.create(user=w[name], classes=every)
    if scenario == "alice+bob rule":
        LearningConsent.objects.filter(user=w["bob"]).update(standing_rules=[{"capability": "proj"}])
    if scenario == "carol restricted":
        LearningConsent.objects.filter(user=w["carol"]).update(workspaces=["elsewhere"], include_new=False)
    if scenario == "carol left":
        WorkspaceMembership.objects.filter(user=w["carol"]).delete()
    srcs = _world(w)
    if scenario == "bob excluded":
        for s in srcs[::2]:
            if s.participants.filter(user=w["bob"]).exists():
                consent.exclude(w["bob"], s)
    # an extra mix: one sealed, one withdrawn, one with a contact
    from apps.contacts.models import Contact

    Session.objects.filter(pk=srcs[0].session_id).update(metadata={rr.METADATA_KEY: ["x"]})
    BrainSource.objects.filter(pk=srcs[1].pk).update(state=BrainSource.WITHDRAWN)
    SourceParticipant.objects.create(source=srcs[2], contact=Contact.objects.create(workspace=w["ws"], email="c@x.org"))
    subj = w[subject] if subject else None
    want = {s.pk for s in BrainSource.objects.all() if eligible(s, w[cap], purpose, subj)}
    got = set(eligible_sources(w[cap], purpose, subj).values_list("pk", flat=True))
    assert got == want, (scenario, cap)
    if scenario == "all" and cap == "proj":
        assert want                                                # the table is not vacuously empty
    assert classes[cap]


def test_a_disabled_or_undeclared_feature_has_no_sources(w):
    LearningConsent.objects.update(classes=["project"])
    capability("dark", "project", enabled=False)
    assert not eligible_sources("dark", "learn").exists()
    assert not eligible_sources("proj", "nope").exists()
    assert not eligible_sources("missing", "learn").exists()
    BrainCapability.objects.filter(slug="proj").update(source_kinds=[])
    assert not eligible_sources("proj", "learn").exists()
    assert not eligible(w["source"], "proj", "learn")
