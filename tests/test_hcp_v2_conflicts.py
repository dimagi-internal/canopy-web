"""HCP v2 conflicts, standing, confirmation and relationships (4.2.3, 3.8, 4.7, 15).

* a conflict is a `contradicts` relationship, recorded on both entries by the provider (the
  Memory Manager) and removed when a revision ends it;
* standing, not the declaration type alone, decides who is held: an agent's "the person said it"
  is unverified until the person confirms it;
* the person's confirmation is a record on one version; it changes neither declaration type nor asserter;
* `derivedFrom` may name another entry or a source, and a client may name only what its grant reads.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.hcp import v2
from apps.contacts.models import EntryConfirmation, EntryRelationship, PersonAuditEvent, PersonFact
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(agent_memory_on):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    login = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    agent.user = login
    agent.save(update_fields=["user"])
    from apps.canopy_sessions.models import Session
    from apps.harness import initiator as who
    from apps.harness import services
    from apps.harness.models import Turn

    session = Session.objects.create(agent=agent, created_by=lili, workspace=ws)
    turn, _ = services.enqueue_turn(session=session, origin=Turn.ORIGIN_API, idempotency_key="t", prompt="hi",
                                    initiator=who.for_user(lili, via="chat", assurance=who.SESSION))
    return {"ws": ws, "lili": lili, "person": contacts.person_for(user=lili), "ada": agent, "turn": turn}


def _add(w, statement, *, basis="declared", by="person", dimension="channel", confidence=None, model="m1",
         category="general_preferences"):
    actor = {"person": hcp.user_actor(w["lili"]), "agent": hcp.agent_actor(w["ada"])}.get(by, hcp.SYSTEM)
    return people.record_fact(
        person=w["person"], workspace=w["ws"], kind="preference", statement=statement, basis=basis,
        category=category, dimension=dimension, confidence=confidence, actor=actor,
        by_user=w["lili"] if by == "person" else None, by_agent=w["ada"] if by == "agent" else None,
        source_turn=w["turn"] if by == "agent" else None,
        captured_by=f"agent:ada/model:{model}" if basis == "inferred" else "")


def _grant(w, *scopes):
    from apps.contacts.models import PersonGrant

    return PersonGrant.objects.create(person=w["person"], workspace=w["ws"], agent=w["ada"], client_key="k",
                                      client_name="Ada", scopes=list(scopes), grant_type="persistent")


def _status(*facts):
    out = []
    for f in facts:
        f.refresh_from_db()
        out.append(f.status)
    return out


def _events(w, kind):
    return list(PersonAuditEvent.objects.filter(person=w["person"], event_type=kind).order_by("pk"))


def _json(c, method, path, body):
    return getattr(c, method)(path, data=json.dumps(body), content_type="application/json")


def test_a_persons_own_declaration_has_standing_and_holds_only_the_inference(w):
    d = _add(w, "Prefers email.")
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    assert _status(d, i) == ["active", "conflicted"]
    assert d.assurance == "authenticated"
    rows = EntryRelationship.objects.filter(type="contradicts")
    assert {(r.entry_id, r.target_entry_id) for r in rows} == {(d.entry_id, i.entry_id), (i.entry_id, d.entry_id)}
    assert {r.asserted_by_role for r in rows} == {"hcp-provider"}      # never taken from a request
    ev = _events(w, "conflict.detected")[0]
    assert (ev.entry_id, ev.related_entry_id) == (i.entry_id, d.entry_id)


def test_an_agents_claim_that_the_person_said_it_has_no_standing_until_they_confirm_it(w):
    d = _add(w, "Prefers email.", by="agent")
    assert d.assurance == "unverified" and d.basis == "declared"
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    # Neither prevails: both are held for the person (4.7.2).
    assert _status(d, i) == ["conflicted", "conflicted"]
    assert not any(hcp.servable(PersonFact.objects.filter(person=w["person"])))
    # The person confirms the declaration: it is active again and has standing; the inference stays held.
    d.refresh_from_db()
    hcp.confirmations.confirm(d, "confirmed", actor=hcp.user_actor(w["lili"]))
    assert _status(d, i) == ["active", "conflicted"]
    d.refresh_from_db()
    assert d.basis == "declared" and d.assurance == "unverified"       # a confirmation rewrites nothing (3.8)


def test_a_confirmation_belongs_to_one_version_and_a_revision_starts_with_none(w):
    d = _add(w, "Prefers email.", by="agent", dimension="")
    me = hcp.user_actor(w["lili"])
    hcp.confirmations.confirm(d, "confirmed", actor=me)
    assert hcp.confirmations.render(d)[0]["disposition"] == "confirmed"
    assert hcp.to_entry(d, redact=False)["record"]["provenance"]["userVerified"] is True
    new = hcp.update_entry(d, statement="Prefers email, always.", reason="reworded", actor=hcp.agent_actor(w["ada"]),
                           by_user=None, source_turn=w["turn"])
    assert hcp.confirmations.render(new) == []
    assert hcp.to_entry(new, redact=False)["record"]["provenance"]["userVerified"] is False
    assert EntryConfirmation.objects.filter(fact__entry_id=d.entry_id).count() == 1


def test_only_the_person_confirms_and_a_confirmation_is_audited(w):
    d = _add(w, "Prefers email.", by="agent", dimension="")
    with pytest.raises(hcp.HcpError):
        hcp.confirmations.confirm(d, "confirmed", actor=hcp.agent_actor(w["ada"]))
    with pytest.raises(hcp.HcpError):
        hcp.confirmations.confirm(d, "maybe", actor=hcp.user_actor(w["lili"]))
    hcp.confirmations.confirm(d, "confirmed", actor=hcp.user_actor(w["lili"]))
    assert "disposition=confirmed" in _events(w, "context.confirmed")[0].detail


def test_rejecting_withdraws_it_and_releases_what_it_held(w):
    d = _add(w, "Prefers email.", by="agent")
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    d.refresh_from_db()
    hcp.confirmations.confirm(d, "rejected", actor=hcp.user_actor(w["lili"]))
    assert _status(d, i) == ["deleted", "active"]
    assert [e.detail for e in _events(w, "conflict.resolved")][0].startswith("transition=rejected")


def test_accepting_a_held_entry_deprecates_the_authoritative_one_but_never_an_attestation(w):
    d = _add(w, "Prefers email.")
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    i.refresh_from_db()
    hcp.confirmations.confirm(i, "confirmed", actor=hcp.user_actor(w["lili"]))
    assert _status(d, i) == ["deprecated", "active"]
    # An attestation is authoritative against an inference and survives the person's acceptance.
    a = _add(w, "Prefers Teams.", basis="attested", by="system", dimension="channel2")
    j = _add(w, "Prefers Zoom.", basis="inferred", by="agent", confidence="low", dimension="channel2", model="m2")
    assert _status(a, j) == ["active", "conflicted"]
    j.refresh_from_db()
    hcp.confirmations.confirm(j, "confirmed", actor=hcp.user_actor(w["lili"]))
    assert _status(a, j) == ["active", "active"]


def test_two_inferences_by_the_same_asserter_set_the_earlier_aside_and_different_asserters_are_both_held(w):
    first = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    second = _add(w, "Prefers Teams.", basis="inferred", by="agent", confidence="low")
    assert _status(first, second) == ["deprecated", "active"]
    assert [e.detail for e in _events(w, "conflict.resolved")] == ["transition=asserter-superseded"]
    third = _add(w, "Prefers Zoom.", basis="inferred", by="agent", confidence="low", model="other")
    assert _status(second, third) == ["conflicted", "conflicted"]


def test_sharing_a_dimension_alone_is_not_a_conflict(w):
    _add(w, "Prefers email.")
    same = _add(w, "prefers   EMAIL", basis="inferred", by="agent", confidence="low")
    assert same.status == "active" and not _events(w, "conflict.detected")      # it only repeats it
    other = _add(w, "Likes short replies.", dimension="length")
    two_declared = _add(w, "Likes long replies.", dimension="length")
    assert two_declared.status == other.status == "active" and not _events(w, "conflict.detected")


def test_a_revision_that_no_longer_conflicts_removes_the_relationship_and_releases_the_entry(w):
    d = _add(w, "Prefers email.")
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    assert i.status == "conflicted"
    i.refresh_from_db()
    hcp.update_entry(i, statement="Prefers Slack for urgent things.", reason="narrower", actor=hcp.agent_actor(w["ada"]),
                     dimension="urgency", source_turn=w["turn"])
    i = hcp.current(i.entry_id)
    assert not EntryRelationship.objects.filter(type="contradicts").exists()
    assert _status(d, i) == ["active", "active"]
    assert _events(w, "conflict.resolved")[-1].detail.startswith("transition=superseded")


def test_hard_delete_leaves_no_relationship_or_confirmation_behind(w):
    d = _add(w, "Prefers email.")
    i = _add(w, "Prefers Slack.", basis="inferred", by="agent", confidence="low")
    i.refresh_from_db()
    hcp.confirmations.confirm(i, "confirmed", actor=hcp.user_actor(w["lili"]))
    hcp.delete_entry(i, actor=hcp.user_actor(w["lili"]), reason="gone", hard=True, by_user=w["lili"])
    assert not EntryRelationship.objects.exists() and not EntryConfirmation.objects.exists()
    d.refresh_from_db()


def test_derived_from_names_an_entry_or_a_source_and_the_provider_records_who(w):
    base = _add(w, "Works on KC.", dimension="", category="work_context")
    specs = [{"entryId": f"urn:uuid:{base.entry_id}"}, {"source": {"kind": "session", "ref": "123"}}]
    new = hcp.add_entry(person=w["person"], workspace=w["ws"], category="work_context", statement="Leads KC metrics.",
                        declaration="model-inferred", confidence="medium", actor=hcp.agent_actor(w["ada"]),
                        derived_from=specs, source="turn:x", source_turn=w["turn"],
                        grant=_grant(w, "hcp:work_context:read", "hcp:work_context:write"))
    rels = v2.to_v2_entry(new)["record"]["relationships"]
    assert {r["target"] for r in rels} == {f"urn:uuid:{base.entry_id}", "urn:canopy:source:session:123"}
    assert {r["type"] for r in rels} == {"derivedFrom"}
    assert {(r["assertedBy"]["role"], r["assertedBy"]["id"]) for r in rels} == {("context-client", "agent:ada")}
    assert all(r["recordedAt"] for r in rels)


def test_a_client_may_not_name_an_entry_its_grant_cannot_read_and_is_not_told_whether_it_exists(w):
    other = _add(w, "Likes terse replies.", dimension="", category="general_preferences")
    grant = _grant(w, "hcp:work_context:read", "hcp:work_context:write")
    kwargs = dict(person=w["person"], workspace=w["ws"], category="work_context", statement="Leads KC metrics.",
                  declaration="model-inferred", confidence="medium", actor=hcp.agent_actor(w["ada"]),
                  source="turn:x", grant=grant, source_turn=w["turn"])
    for target in (f"urn:uuid:{other.entry_id}", "urn:uuid:00000000-0000-4000-8000-000000000000"):
        with pytest.raises(hcp.HcpError) as e:
            hcp.add_entry(derived_from=[{"entryId": target}], **kwargs)
        assert e.value.problem == "scope-denied"
    with pytest.raises(hcp.HcpError) as e:
        hcp.add_entry(derived_from=[{"source": {"kind": "Not A Kind", "ref": "1"}}], **kwargs)
    assert e.value.problem == "malformed-request"


def test_the_confirmation_route_is_the_persons_and_refuses_an_agent_login(w):
    d = _add(w, "Prefers email.", by="agent", dimension="")
    url = f"/api/hcp/v2/entries/urn:uuid:{d.entry_id}/confirmation"
    c = Client()
    c.force_login(w["lili"])
    ok = _json(c, "post", url, {"disposition": "confirmed", "expectedVersion": 1})
    assert ok.status_code == 200, ok.content
    body = ok.json()
    assert body["record"]["provenance"]["confirmations"][0]["disposition"] == "confirmed"
    assert body["assertion"]["declarationType"] == "person-declared"
    stale = _json(c, "post", url, {"disposition": "confirmed", "expectedVersion": 9})
    assert stale.status_code == 409 and stale.json()["type"].endswith("version-conflict")
    a = Client()
    a.force_login(w["ada"].user)
    assert _json(a, "post", url, {"disposition": "rejected"}).status_code == 403
    assert hcp.confirmations.latest(d) == "confirmed"


def test_discovery_declares_how_conflicts_are_found(w):
    assert hcp.CONFLICT_DETECTION == "structural"
