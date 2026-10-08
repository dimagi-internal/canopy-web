"""The fleet brain v1 (canopy#804): facts, digests, the envelope `person` block,
the /api/people routes and their privacy gates, and the forced digest turn.

The privacy tests are the point of this file. The rules they pin:

* facts are read and written per workspace, by its members, and a workspace
  never serves another workspace's facts (Q2);
* a person a workspace does not deal with is "not found" there;
* the person can see — and retract — everything held about them;
* conversations are readable by the agent they were with (its own login) and
  nobody new: another agent's login is refused;
* the digest turn fires for a human's finished turn only, never for itself, for
  canopy/agent-started work, or without an agent, and is debounced.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import Contact, PersonAccess, PersonFact
from apps.harness import caller_context, people_digest, services
from apps.harness import initiator as who
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


# --- fixtures -----------------------------------------------------------------------


def _member(ws, username, role=WorkspaceMembership.EDITOR, **kw):
    u = User.objects.create_user(username, f"{username}@dimagi.com", "pw", **kw)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    return u


def _agent(ws, slug, owner, *, login=True):
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
    if login:
        u = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
        agent.user = u
        agent.save(update_fields=["user"])
    return agent


@pytest.fixture(autouse=True)
def _digest_on(settings):
    # The switch is OFF in production since 2026-10-07; these tests pin what it does ON.
    settings.PEOPLE_DIGEST_ENABLED = True


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    lili = _member(ws, "lili", first_name="Lilianna", last_name="Bagnoli")
    ace = _agent(ws, "ace", owner)
    hal = _agent(ws, "hal", owner)
    eva = _agent(other, "eva", owner)
    return {"owner": owner, "ws": ws, "other": other, "lili": lili,
            "ace": ace, "hal": hal, "eva": eva}


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _human_turn(agent, user, key, **kw):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=kw.pop("prompt", "hi"),
        initiator=who.for_user(user, via="chat", assurance=who.SESSION), **kw)
    return turn


def _finish(turn, status=Turn.DONE):
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    return services.finish_turn(turn, status=status)


def _digest_turns():
    return Turn.objects.filter(idempotency_key__startswith=people_digest.KEY_PREFIX)


def _fact(person, ws, kind="role", statement="Program lead for KC.", **kw):
    return people.record_fact(person=person, workspace=ws, kind=kind, statement=statement, **kw)


# --- who a person is ----------------------------------------------------------------


def test_person_for_a_user_is_idempotent_and_joins_their_verified_address(world):
    from allauth.account.models import EmailAddress

    lili = world["lili"]
    p1 = contacts.person_for(user=lili)
    assert contacts.person_for(user=lili) == p1
    assert p1.user == lili

    # A correspondent canopy met by email first is the SAME person once a login
    # proves the address.
    u = User.objects.create_user("fatima", "fatima@llo.org", "pw")
    c = contacts.record_inbound_sender(workspace=world["ws"], address="fatima@llo.org")
    EmailAddress.objects.create(user=u, email="fatima@llo.org", verified=True, primary=True)
    assert contacts.person_for(user=u) == c.person
    assert contacts.person_for(contact=c) == c.person


def test_an_unverified_user_email_does_not_join_a_correspondent(world):
    c = contacts.record_inbound_sender(workspace=world["ws"], address="mallory@llo.org")
    u = User.objects.create_user("mallory", "mallory@llo.org", "pw")  # no verified address
    assert contacts.person_for(user=u) != c.person


def test_a_slack_contact_gets_a_person_of_its_own(world):
    c = contacts.record_slack_user(workspace=world["ws"], team_id="T1", slack_user_id="U1",
                                   email="x@partner.org")
    assert c.person is None
    p = contacts.person_for(contact=c)
    assert p is not None and p.email == ""          # keyless: matches nobody else
    c.refresh_from_db()
    assert c.person == p and contacts.person_for(contact=c) == p


# --- the envelope --------------------------------------------------------------------


def test_envelope_v3_carries_the_person_and_logs_the_read(world):
    ws, lili, ace = world["ws"], world["lili"], world["ace"]
    person = contacts.person_for(user=lili)
    _fact(person, ws, "role", "Program lead for KC.")
    _fact(person, ws, "correction", "Say KC (kangaroo care), not KMC.")
    _fact(person, ws, "terminology", "Calls the OCS bot 'the coach'.")
    people.put_digest(person=person, workspace=ws, text="Lilianna runs KC.", by_agent=ace)

    env = caller_context.build(_human_turn(ace, lili, "t1"))
    assert env["version"] == 3
    block = env["person"]
    assert block["id"] == person.pk
    assert block["display_name"] == "Lilianna Bagnoli"
    assert block["email"] == "lili@dimagi.com"
    assert block["workspace"] == "connect"
    assert block["see_all"] == "/people/me/"
    assert block["digest"] == "Lilianna runs KC."
    assert block["digest_updated_at"]
    assert block["facts"][0]["kind"] == "correction"            # corrections first
    assert [f["kind"] for f in block["facts"][1:]] == ["terminology", "role"]  # then newest
    assert env["contact"] is None                                # unchanged for members
    access = PersonAccess.objects.get(person=person)
    assert (access.via, access.reader_agent, access.workspace_id) == ("envelope", ace, "connect")


def test_facts_of_one_workspace_are_not_served_in_anothers_envelope(world):
    ws, other, lili, eva = world["ws"], world["other"], world["lili"], world["eva"]
    WorkspaceMembership.objects.create(user=lili, workspace=other, role=WorkspaceMembership.EDITOR)
    person = contacts.person_for(user=lili)
    _fact(person, ws, "correction", "Say KC, not KMC.")
    people.put_digest(person=person, workspace=ws, text="connect-only digest")

    env = caller_context.build(_human_turn(eva, lili, "t-eva"))
    assert env["person"]["id"] == person.pk
    assert env["person"]["workspace"] == "dimagi"
    assert env["person"]["facts"] == []
    assert env["person"]["digest"] == ""


def test_person_is_null_for_canopy_and_for_another_agents_login(world):
    ace, hal = world["ace"], world["hal"]
    sched, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="s",
                                     initiator=who.system(via="schedule"))
    assert caller_context.build(sched)["person"] is None
    dispatched = _human_turn(ace, hal.user, "from-hal")
    assert caller_context.build(dispatched)["person"] is None
    assert not PersonAccess.objects.exists()


def test_a_contact_has_a_person_block_too(world):
    from apps.agents.testing import admit_contacts

    ace = world["ace"]
    admit_contacts(ace)
    turn, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                                    origin_ref={"from": "fatima@llo.org", "subject": "hi"})
    env = caller_context.build(turn)
    contact = Contact.objects.get(email="fatima@llo.org")
    assert env["contact"]["id"] == contact.pk
    assert env["person"]["id"] == contact.person_id
    assert env["person"]["email"] == "fatima@llo.org"


def test_the_digest_turn_says_so_in_its_envelope(world):
    turn = _finish(_human_turn(world["ace"], world["lili"], "t1"))
    digest = _digest_turns().get()
    env = caller_context.build(digest)
    assert env["trigger"]["kind"] == "people_digest"
    assert env["person"] is None and env["relationship"] == "system"
    assert caller_context.build(turn)["trigger"]["kind"] is None


# --- facts: rules --------------------------------------------------------------------


def test_unknown_kind_and_bad_statements_are_rejected(world):
    person = contacts.person_for(user=world["lili"])
    for kind, statement in (("health", "Has a cold."), ("role", ""), ("role", "x" * 501)):
        with pytest.raises(people.FactError):
            _fact(person, world["ws"], kind, statement)


def test_the_database_refuses_an_unknown_kind_too(world):
    from django.db import IntegrityError, transaction

    person = contacts.person_for(user=world["lili"])
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonFact.objects.create(person=person, workspace=world["ws"], kind="sentiment",
                                  statement="Seems annoyed.")


def test_supersede_marks_the_old_fact_and_only_within_person_and_workspace(world):
    ws, other = world["ws"], world["other"]
    person = contacts.person_for(user=world["lili"])
    old = _fact(person, ws, "preference", "Prefers email.")
    new = _fact(person, ws, "preference", "Prefers Slack.", supersedes=old)
    old.refresh_from_db()
    assert old.superseded_at is not None and new.supersedes == old
    assert list(people.live_facts(person, ws.pk)) == [new]
    with pytest.raises(people.FactError):  # already superseded
        _fact(person, ws, "preference", "Prefers phone.", supersedes=old)
    elsewhere = _fact(person, other, "role", "Advisor.")
    with pytest.raises(people.FactError):  # another workspace's fact
        _fact(person, ws, "role", "Lead.", supersedes=elsewhere)


# --- the API -------------------------------------------------------------------------


def test_a_member_reads_a_person_and_the_read_is_logged(world):
    person = contacts.person_for(user=world["lili"])
    _fact(person, world["ws"], "role", "KC lead.")
    r = _client(world["owner"]).get(f"/api/people/{person.pk}/?workspace=connect")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["facts"][0]["statement"] == "KC lead." and body["workspace"] == "connect"
    a = PersonAccess.objects.get(person=person)
    assert (a.via, a.reader_user) == ("api", world["owner"])


def test_a_non_member_is_denied_everything(world):
    person = contacts.person_for(user=world["lili"])
    fact = _fact(person, world["ws"], "role", "KC lead.")
    stranger = User.objects.create_user("zz", "zz@else.org", "pw")
    c = _client(stranger)
    assert c.get(f"/api/people/{person.pk}/?workspace=connect").status_code == 404
    assert c.get("/api/people/lookup/?email=lili@dimagi.com").status_code == 404
    r = c.post(f"/api/people/{person.pk}/facts/",
               data={"workspace": "connect", "kind": "role", "statement": "x"},
               content_type="application/json")
    assert r.status_code == 404
    assert c.put(f"/api/people/{person.pk}/digest/", data={"workspace": "connect", "text": "x"},
                 content_type="application/json").status_code == 404
    assert c.post(f"/api/people/{person.pk}/facts/{fact.pk}/retract/").status_code == 404
    assert c.get(f"/api/people/{person.pk}/conversations/?agent=ace").status_code == 404
    assert not PersonAccess.objects.exists()


def test_a_person_unknown_to_the_workspace_is_not_found_there(world):
    """A member of `dimagi` cannot learn that `connect` knows someone."""
    outsider = contacts.record_inbound_sender(workspace=world["ws"], address="p@llo.org").person
    dimagi_only = _member(world["other"], "dee")
    c = _client(dimagi_only)
    assert c.get(f"/api/people/{outsider.pk}/?workspace=dimagi").status_code == 404
    assert c.get("/api/people/lookup/?email=p@llo.org").status_code == 404
    r = c.post(f"/api/people/{outsider.pk}/facts/",
               data={"workspace": "dimagi", "kind": "role", "statement": "Spy."},
               content_type="application/json")
    assert r.status_code == 404
    # ...while connect's members find them.
    r = _client(world["owner"]).get("/api/people/lookup/?email=p@llo.org")
    assert r.status_code == 200 and r.json()["id"] == outsider.pk


def test_an_agent_login_writes_facts_as_the_agent_and_unknown_kinds_are_400(world):
    person = contacts.person_for(user=world["lili"])
    turn = _human_turn(world["ace"], world["lili"], "src")
    project = AgentProject.objects.create(agent=world["ace"], ext_id="P7", name="Kangaroo Care")
    c = _client(world["ace"].user)
    r = c.post(f"/api/people/{person.pk}/facts/", content_type="application/json", data={
        "workspace": "connect", "kind": "project", "statement": "Works on KC coaching.",
        "basis": "inferred", "source_turn_id": str(turn.pk), "project_id": project.pk,
        "instance_ref": "OCS bot 'KMC Audit'"})
    assert r.status_code == 201, r.content
    fact = PersonFact.objects.get(pk=r.json()["id"])
    assert fact.asserted_by_agent == world["ace"] and fact.asserted_by_user is None
    assert fact.source_turn == turn and fact.project == project
    assert r.json()["project"] == {"id": project.pk, "title": "Kangaroo Care", "ext_id": "P7"}

    bad = c.post(f"/api/people/{person.pk}/facts/", content_type="application/json",
                 data={"workspace": "connect", "kind": "health", "statement": "Has a cold."})
    assert bad.status_code == 400
    # A project from another workspace is refused.
    eva_p = AgentProject.objects.create(agent=world["eva"], ext_id="P1", name="Other")
    bad = c.post(f"/api/people/{person.pk}/facts/", content_type="application/json",
                 data={"workspace": "connect", "kind": "project", "statement": "x",
                       "project_id": eva_p.pk})
    assert bad.status_code == 400


def test_supersede_over_the_api(world):
    person = contacts.person_for(user=world["lili"])
    old = _fact(person, world["ws"], "terminology", "Says KMC.")
    r = _client(world["ace"].user).post(
        f"/api/people/{person.pk}/facts/", content_type="application/json",
        data={"workspace": "connect", "kind": "correction", "statement": "Say KC, not KMC.",
              "supersedes_id": old.pk})
    assert r.status_code == 201 and r.json()["supersedes_id"] == old.pk
    old.refresh_from_db()
    assert old.superseded_at is not None


def test_the_person_can_retract_their_own_fact_and_others_cannot(world):
    person = contacts.person_for(user=world["lili"])
    fact = _fact(person, world["ws"], "role", "Wrong role.", by_agent=world["ace"])
    nosy = _member(world["ws"], "nosy")
    assert _client(nosy).post(
        f"/api/people/{person.pk}/facts/{fact.pk}/retract/").status_code == 404
    r = _client(world["lili"]).post(f"/api/people/{person.pk}/facts/{fact.pk}/retract/")
    assert r.status_code == 200, r.content
    fact.refresh_from_db()
    assert fact.retracted_at is not None and fact.retracted_by == world["lili"]
    assert list(people.live_facts(person, "connect")) == []


def test_the_asserter_and_a_workspace_admin_may_retract(world):
    person = contacts.person_for(user=world["lili"])
    by_ace = _fact(person, world["ws"], "role", "A.", by_agent=world["ace"])
    by_owner = _fact(person, world["ws"], "role", "B.")
    assert _client(world["ace"].user).post(
        f"/api/people/{person.pk}/facts/{by_ace.pk}/retract/").status_code == 200
    assert _client(world["owner"]).post(
        f"/api/people/{person.pk}/facts/{by_owner.pk}/retract/").status_code == 200
    # Another agent's login did not assert it and is no admin.
    third = _fact(person, world["ws"], "role", "C.", by_agent=world["ace"])
    assert _client(world["hal"].user).post(
        f"/api/people/{person.pk}/facts/{third.pk}/retract/").status_code == 404


def test_put_digest_and_me_shows_everything(world):
    lili = world["lili"]
    person = contacts.person_for(user=lili)
    _fact(person, world["ws"], "correction", "Say KC.")
    r = _client(world["ace"].user).put(
        f"/api/people/{person.pk}/digest/", content_type="application/json",
        data={"workspace": "connect", "text": "Runs KC coaching.", "source_turn_ids": ["a"]})
    assert r.status_code == 200 and r.json()["updated_by"] == "ace"
    caller_context.build(_human_turn(world["ace"], lili, "t"))

    me = _client(lili).get("/api/people/me/").json()
    assert me["id"] == person.pk and me["display_name"] == "Lilianna Bagnoli"
    assert [f["statement"] for f in me["facts"]] == ["Say KC."]
    assert me["facts"][0]["workspace"] == "connect"
    assert me["digests"] == [{"workspace": "connect", "text": "Runs KC coaching.",
                              "updated_at": me["digests"][0]["updated_at"], "updated_by": "ace"}]
    assert me["accesses"][0]["via"] == "envelope" and me["accesses"][0]["reader_agent"] == "ace"


# --- conversations -------------------------------------------------------------------


def test_the_agent_reads_its_own_conversations_with_a_person(world):
    lili, ace, hal = world["lili"], world["ace"], world["hal"]
    person = contacts.person_for(user=lili)
    mine = _human_turn(ace, lili, "c1", prompt="Is the KC coach live?")
    _human_turn(hal, lili, "c2", prompt="private to hal")
    _human_turn(ace, world["owner"], "c3", prompt="someone else")

    r = _client(ace.user).get(f"/api/people/{person.pk}/conversations/?agent=ace")
    assert r.status_code == 200, r.content
    rows = r.json()["conversations"]
    assert [row["id"] for row in rows] == [str(mine.pk)]
    assert rows[0]["prompt"] == "Is the KC coach live?"
    assert PersonAccess.objects.filter(person=person, via="api", reader_agent=ace).exists()


def test_another_agent_cannot_read_an_agents_conversations(world):
    person = contacts.person_for(user=world["lili"])
    _human_turn(world["ace"], world["lili"], "c1", prompt="secret")
    r = _client(world["hal"].user).get(f"/api/people/{person.pk}/conversations/?agent=ace")
    assert r.status_code == 403
    r = _client(world["lili"]).get(f"/api/people/{person.pk}/conversations/?agent=ace")
    assert r.status_code == 403  # a plain member is not the agent


def test_conversations_since_filters(world):
    person = contacts.person_for(user=world["lili"])
    t = _human_turn(world["ace"], world["lili"], "old")
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(days=3))
    _human_turn(world["ace"], world["lili"], "new")
    since = (timezone.now() - dt.timedelta(days=1)).isoformat()
    r = _client(world["ace"].user).get(f"/api/people/{person.pk}/conversations/",
                                       {"agent": "ace", "since": since})
    assert r.status_code == 200 and len(r.json()["conversations"]) == 1


# --- the digest turn -----------------------------------------------------------------


def test_a_finished_human_turn_enqueues_a_digest_turn(world):
    ace, lili = world["ace"], world["lili"]
    person = contacts.person_for(user=lili)
    turn = _finish(_human_turn(ace, lili, "t1"))
    digest = _digest_turns().get()
    assert digest.agent == ace
    assert digest.prompt.splitlines()[0].startswith(
        f"/canopy:people-digest --person {person.pk} --workspace connect --since ")
    assert digest.origin_ref["trigger"] == "people_digest"
    assert digest.origin_ref["no_outbound"] is True
    assert digest.initiator_kind == who.SYSTEM
    assert digest.parent_turn == turn


def test_digest_turns_never_retrigger(world):
    _finish(_human_turn(world["ace"], world["lili"], "t1"))
    digest = _digest_turns().get()
    with timezone.override("UTC"):
        Turn.objects.filter(pk=digest.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=5))
    _finish(digest)
    assert _digest_turns().count() == 1


def test_no_digest_for_system_or_agent_initiated_turns(world):
    ace = world["ace"]
    sched, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="s",
                                     initiator=who.system(via="schedule", accountable=world["owner"]))
    _finish(sched)
    agent_turn, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="a",
                                          initiator=who.for_agent("hal", via="dispatch"))
    _finish(agent_turn)
    _finish(_human_turn(ace, world["hal"].user, "hal-login"))  # another agent's LOGIN
    assert not _digest_turns().exists()


def test_no_digest_without_an_agent(world):
    turn, _ = services.enqueue_turn(
        project="canopy-web", workspace=world["ws"], origin=Turn.ORIGIN_API, idempotency_key="p",
        initiator=who.for_user(world["lili"], via="chat", assurance=who.SESSION))
    _finish(turn)
    assert not _digest_turns().exists()


def test_no_digest_for_a_failed_turn(world):
    t = _human_turn(world["ace"], world["lili"], "f")
    Turn.objects.filter(pk=t.pk).update(session_key="s1")
    _finish(t, Turn.FAILED)
    assert not _digest_turns().exists()


def test_the_digest_turn_is_debounced_per_agent_and_person(world, settings):
    ace, hal, lili = world["ace"], world["hal"], world["lili"]
    _finish(_human_turn(ace, lili, "t1"))
    _finish(_human_turn(ace, lili, "t2"))
    assert _digest_turns().filter(agent=ace).count() == 1
    # Another agent, or another person, is its own pair.
    _finish(_human_turn(hal, lili, "t3"))
    _finish(_human_turn(ace, world["owner"], "t4"))
    assert _digest_turns().count() == 3
    # Past the window, the next one fires and looks back to the last digest.
    first = _digest_turns().filter(agent=ace, origin_ref__person_id=contacts.person_for(user=lili).pk).get()
    past = timezone.now() - dt.timedelta(minutes=settings.PEOPLE_DIGEST_DEBOUNCE_MINUTES + 1)
    Turn.objects.filter(pk=first.pk).update(created_at=past)
    _finish(_human_turn(ace, lili, "t5"))
    newest = _digest_turns().filter(agent=ace).order_by("-created_at").first()
    assert newest.pk != first.pk
    assert newest.origin_ref["since"] == past.isoformat()


def test_the_kill_switch(world, settings):
    settings.PEOPLE_DIGEST_ENABLED = False
    _finish(_human_turn(world["ace"], world["lili"], "t1"))
    assert not _digest_turns().exists()


def test_a_digest_turn_yields_to_a_persons_next_message_at_claim(world):
    from apps.agents.models import AgentAdmin

    ace, owner = world["ace"], world["owner"]
    runner = Runner.objects.create(name="box", kind=Runner.EMDASH, host="box", owner=owner,
                                   workspace_id="connect", status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(),
                                   capabilities={"sessions": True})
    RunnerAssignment.objects.create(agent=ace, runner=runner, rank=0)
    AgentAdmin.objects.get_or_create(agent=ace, user=owner)
    _finish(_human_turn(ace, world["lili"], "t1"))
    assert _digest_turns().count() == 1
    follow_up = _human_turn(ace, world["lili"], "t2")
    claimed = services.claim_next_turn(runner)
    assert claimed.pk == follow_up.pk
