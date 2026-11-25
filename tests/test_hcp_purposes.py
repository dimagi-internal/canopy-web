"""Purposes (HCP v2 6.1.5.2, 4.2.6): what a read is for travels with the grant and the entry.

* every grant carries at least one purpose, shown to the person before the act;
* every agent read names one (a v1 caller names none, and the adapter gives it the
  grant's own default, so v1 callers keep working unchanged);
* a read naming a purpose the grant does not carry is refused, and an entry limited to
  other purposes (by its writer, or by the person) is not served for this one;
* only the person limits an entry's purposes, only ever narrowing, and it is audited.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.hcp import purposes
from apps.contacts.models import PersonAuditEvent, PersonFact, PersonGrant
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(agent_memory_on):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    person = contacts.person_for(user=lili)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    login = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])
    return {"ws": ws, "lili": lili, "person": person, "ada": agent}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _json(c, method, path, body=None):
    return getattr(c, method)(path, data=json.dumps(body or {}), content_type="application/json")


def _session(w):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(agent=w["ada"], created_by=w["lili"], workspace=w["ws"])


def _grant(w, session, features=("record", "use")):
    r = _json(_as(w["lili"]), "post", f"/api/people/me/sessions/{session.pk}/agent-grants/",
              {"features": list(features), "duration": "session", "surface": "chat"})
    assert r.status_code == 200, r.content
    return PersonGrant.objects.filter(person=w["person"]).order_by("-pk").first()


def _turn(w, session, key="t1"):
    turn, _ = services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_API, idempotency_key=key, prompt="KC metrics",
        initiator=who.for_user(w["lili"], via="chat", assurance=who.SESSION))
    return turn


def _search(w, turn, **extra):
    body = {"query": "KC metrics", "categories": ["work_context"], "purpose": "answer", **extra}
    return _json(_as(w["ada"].user), "post", f"/api/hcp/v1/preferences/search?turn={turn.pk}", body)


def _fact(w, statement="KC metrics lead.", **kw):
    return people.record_fact(person=w["person"], workspace=w["ws"], kind="role", statement=statement,
                              basis="declared", **kw)


def test_the_registry_describes_every_purpose_in_words_a_person_reads():
    for code in (purposes.ASSISTANCE, purposes.MEMORY_KEEPING, purposes.COACHING):
        assert code.startswith("https://") and purposes.PURPOSES[code].strip()
    assert [d["code"] for d in purposes.describe([purposes.ASSISTANCE])] == [purposes.ASSISTANCE]
    with pytest.raises(hcp.HcpError):
        purposes.check_codes(["https://example.com/not-registered"])
    with pytest.raises(hcp.HcpError):
        purposes.check_codes([])


def test_a_grant_carries_the_purposes_of_what_it_allows_and_says_so_when_issued(w):
    session = _session(w)
    grant = _grant(w, session)
    assert set(grant.purposes) == {purposes.ASSISTANCE, purposes.MEMORY_KEEPING}
    shown = hcp.grant_dict(grant)
    assert [p["code"] for p in shown["purposes"]] == purposes.of_grant(grant)
    assert all(p["description"] for p in shown["purposes"])
    issued = PersonAuditEvent.objects.get(person=w["person"], event_type="grant.issued")
    assert "purposes=" in issued.detail and purposes.ASSISTANCE in issued.detail
    # A read-only grant carries only the read purpose.
    session2 = _session(w)
    _json(_as(w["lili"]), "post", f"/api/people/me/sessions/{session2.pk}/agent-grants/",
          {"features": ["use"], "duration": "session", "surface": "chat"})
    read_only = PersonGrant.objects.filter(person=w["person"]).order_by("-pk").first()
    assert read_only.purposes == [purposes.ASSISTANCE]


def test_an_older_grant_with_no_stored_purposes_is_read_as_the_purposes_of_its_scopes(w):
    session = _session(w)
    grant = _grant(w, session, features=("use",))
    PersonGrant.objects.filter(pk=grant.pk).update(purposes=[])
    grant.refresh_from_db()
    assert purposes.of_grant(grant) == [purposes.ASSISTANCE]


def test_a_v1_search_names_no_purpose_code_and_is_served_under_the_grants_default(w):
    _fact(w)
    session = _session(w)
    _grant(w, session)
    r = _search(w, _turn(w, session))
    assert r.status_code == 200 and len(r.json()["entries"]) == 1
    read = PersonAuditEvent.objects.filter(person=w["person"], event_type="preference.read").first()
    assert read.purpose_code == purposes.ASSISTANCE
    assert read.purpose == "answer"               # the v1 words stay what the person reads


def test_a_search_naming_a_purpose_the_grant_does_not_carry_is_refused(w):
    _fact(w)
    session = _session(w)
    _grant(w, session)
    r = _search(w, _turn(w, session), purposeCode=purposes.COACHING)
    assert r.status_code == 403
    assert purposes.COACHING in r.json()["detail"]
    r = _search(w, _turn(w, session, "t2"), purposeCode="not a uri")
    assert r.status_code == 422
    ok = _search(w, _turn(w, session, "t3"), purposeCode=purposes.ASSISTANCE)
    assert ok.status_code == 200


def test_an_entry_limited_to_other_purposes_is_not_served_for_this_one(w):
    _fact(w, "Open to everything.")
    limited = _fact(w, "KC metrics only for coaching.", permitted_purposes=[purposes.COACHING])
    session = _session(w)
    _grant(w, session)
    texts = [e["claim"]["subject"]["preference"] for e in _search(w, _turn(w, session)).json()["entries"]]
    assert "KC metrics only for coaching." not in texts and "Open to everything." in texts
    # The person still sees it, whatever its purposes.
    assert limited.pk in {f.pk for f in hcp.current_versions(w["person"])}


def test_the_person_limits_an_entrys_purposes_only_narrowing_and_audited(w):
    fact = _fact(w, permitted_purposes=[purposes.ASSISTANCE, purposes.COACHING])
    person_actor = hcp.user_actor(w["lili"])
    # Names a purpose the writer did not permit: no effect on what is served, so refused outright.
    with pytest.raises(hcp.HcpError):
        hcp.restrict_purposes(fact, [purposes.MEMORY_KEEPING], actor=person_actor)
    narrowed = hcp.restrict_purposes(fact, [purposes.COACHING], actor=person_actor)
    assert narrowed.restricted_purposes == [purposes.COACHING]
    event = PersonAuditEvent.objects.get(person=w["person"], event_type="context.restricted")
    assert event.entry_id == fact.entry_id and purposes.COACHING in event.detail
    # Null removes the person's narrowing, never the writer's.
    cleared = hcp.restrict_purposes(narrowed, None, actor=person_actor)
    assert cleared.restricted_purposes is None and cleared.permitted_purposes == [
        purposes.ASSISTANCE, purposes.COACHING]
    # Only the person (a governance act): an agent is refused.
    with pytest.raises(hcp.HcpError):
        hcp.restrict_purposes(fact, [purposes.COACHING], actor=hcp.agent_actor(w["ada"]))


def test_the_governance_route_is_the_persons_and_refuses_an_agent_login(w):
    fact = _fact(w, permitted_purposes=[purposes.ASSISTANCE, purposes.COACHING])
    url = f"/api/hcp/v2/entries/urn:uuid:{fact.entry_id}/purposes"
    ok = _json(_as(w["lili"]), "put", url, {"permittedPurposes": [purposes.COACHING]})
    assert ok.status_code == 200, ok.content
    assert ok.json()["classification"]["permittedPurposes"] == [purposes.COACHING]
    refused = _json(_as(w["ada"].user), "put", url, {"permittedPurposes": [purposes.ASSISTANCE]})
    assert refused.status_code == 403
    gone = _json(_as(w["lili"]), "put", url, {"permittedPurposes": None})
    assert gone.status_code == 200 and "permittedPurposes" not in gone.json()["classification"] or \
        gone.json()["classification"]["permittedPurposes"] == [purposes.ASSISTANCE, purposes.COACHING]


def test_a_v1_write_may_name_the_purposes_it_permits(w):
    session = _session(w)
    _grant(w, session)
    turn = _turn(w, session)
    r = _json(_as(w["ada"].user), "post", f"/api/hcp/v1/preferences/add?turn={turn.pk}",
              {"category": "work_context", "preference": "Works on KC.", "declarationType": "user-declared",
               "sourceContext": f"turn:{turn.pk}", "permittedPurposes": [purposes.COACHING]})
    assert r.status_code == 201, r.content
    fact = PersonFact.objects.get(statement="Works on KC.")
    assert fact.permitted_purposes == [purposes.COACHING]
    bad = _json(_as(w["ada"].user), "post", f"/api/hcp/v1/preferences/add?turn={turn.pk}",
                {"category": "work_context", "preference": "x y z.", "declarationType": "user-declared",
                 "sourceContext": "t", "permittedPurposes": ["https://example.com/other"]})
    assert bad.status_code == 422


def test_what_the_prompt_shows_before_the_act_names_each_purpose(w):
    session = _session(w)
    body = _as(w["lili"]).get(f"/api/people/me/sessions/{session.pk}/agent-memory/").json()
    shown = {p["code"]: p["description"] for p in body["purposes"]}
    assert shown[purposes.ASSISTANCE] and shown[purposes.MEMORY_KEEPING]
    assert body["purposes_by_feature"] == {"use": [purposes.ASSISTANCE], "record": [purposes.MEMORY_KEEPING]}


def test_an_oauth_clients_grant_carries_the_purposes_it_was_registered_for(w):
    from apps.contacts import hcp_oauth
    from apps.contacts.models import HcpClient

    client = HcpClient.objects.create(client_id="c1", name="App", operator="Acme", redirect_uris=["https://a/cb"],
                                      allowed_scopes=["hcp:work_context:read"],
                                      purposes=[purposes.ASSISTANCE])
    pending = {"client": client.pk, "scopes": ["hcp:work_context:read"], "sources": [], "expiry": "4h"}
    grant = hcp_oauth.issue_grant(pending, person=w["person"], user=w["lili"], persistent=False)
    assert grant.purposes == [purposes.ASSISTANCE]
    assert purposes.ASSISTANCE in PersonAuditEvent.objects.get(grant_id=grant.grant_id,
                                                               event_type="grant.issued").detail
