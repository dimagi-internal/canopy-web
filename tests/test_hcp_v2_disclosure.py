"""Disclosures, use-until, the disclosure summary and the v2 HTTP context plane (HCP v2 7.4, 7.6, 8.6, 9.4).

* use-until: an explicit time first, else 10% of the entry's age, a 1 h floor, 30 d / 24 h ceilings,
  and 1 h for a client that cannot be told of a revocation;
* a client receives a disclosure with a grant-scoped ref, never the stored entry;
* the summary equals what the raw events say;
* the v1 wire and the v2 binding are two views of the same rows.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.hcp import disclosure, purposes, v2
from apps.contacts.models import HcpClient, PersonAuditEvent, PersonFact, PersonGrant
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def w(agent_memory_on):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    login = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(agent=agent, created_by=lili, workspace=ws)
    c = Client()
    c.force_login(lili)
    r = c.post(f"/api/people/me/sessions/{session.pk}/agent-grants/",
               data=json.dumps({"features": ["record", "use"], "duration": "session", "surface": "chat"}),
               content_type="application/json")
    assert r.status_code == 200, r.content
    turn, _ = services.enqueue_turn(session=session, origin=Turn.ORIGIN_API, idempotency_key="t", prompt="KC metrics",
                                    initiator=who.for_user(lili, via="chat", assurance=who.SESSION))
    return {"ws": ws, "lili": lili, "person": contacts.person_for(user=lili), "ada": agent, "turn": turn,
            "grant": PersonGrant.objects.get(person__user=lili)}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _j(c, method, path, body=None):
    return getattr(c, method)(path, data=json.dumps(body or {}), content_type="application/json")


def _fact(w, statement="KC metrics lead.", category="work_context", kind="role", **kw):
    return people.record_fact(person=w["person"], workspace=w["ws"], kind=kind, statement=statement,
                              basis="declared", category=category, by_user=w["lili"], **kw)


def _q(w, **extra):
    body = {"query": "KC metrics", "purpose": purposes.ASSISTANCE, **extra}
    return _j(_as(w["ada"].user), "post", f"/api/hcp/v2/entries/query?turn={w['turn'].pk}", body)


# --- use-until (7.6) -----------------------------------------------------------------------------


def _row(age_hours, expires=None, category="work_context"):
    p = PersonFact(category=category, created_at=NOW - dt.timedelta(hours=age_hours), expires_at=expires)
    return p


@pytest.mark.parametrize("age,expected_hours", [
    (0, 1),                 # the floor
    (5, 1),                 # 10% of 5 h is under the floor
    (50, 5),                # the heuristic
    (24 * 400, 24 * 30),    # the 30 day ceiling
])
def test_use_until_is_a_tenth_of_the_age_between_a_floor_and_a_ceiling(age, expected_hours):
    at = disclosure.use_until(_row(age), now=NOW, notified=True)
    assert at - NOW == dt.timedelta(hours=expected_hours)


def test_an_explicit_time_applies_first_even_below_the_floor_and_is_still_capped():
    soon = NOW + dt.timedelta(minutes=10)
    assert disclosure.use_until(_row(500, expires=soon), now=NOW, notified=True) == soon
    far = NOW + dt.timedelta(days=90)
    assert disclosure.use_until(_row(500, expires=far), now=NOW, notified=True) == NOW + dt.timedelta(days=30)


def test_a_client_that_cannot_be_notified_gets_at_most_an_hour_and_a_high_category_a_day():
    assert disclosure.use_until(_row(24 * 400), now=NOW, notified=False) - NOW == dt.timedelta(hours=1)
    assert disclosure.use_until(_row(24 * 400, category="health_context"), now=NOW, notified=True) - NOW == \
        dt.timedelta(hours=24)
    assert disclosure.sensitivity_of(["general_preferences", "work_context"]) == "medium"
    assert disclosure.sensitivity_of(["hcp-custom:x"]) == "medium"                  # a custom one is never low
    assert disclosure.longest_copy(["work_context"], notified=False) == dt.timedelta(hours=1)
    assert disclosure.longest_copy(["work_context"], notified=True) == dt.timedelta(days=30)


def test_the_first_party_agent_has_no_notification_endpoint_and_a_registered_webhook_has(w):
    assert not disclosure.has_notification_endpoint(w["grant"])
    client = HcpClient.objects.create(client_id="c", name="C", operator="O", redirect_uris=["https://c.o.example/cb"],
                                      webhook_url="https://c.o.example/hook")
    assert disclosure.has_notification_endpoint(PersonGrant(hcp_client=client))


# --- the disclosure (7.4) ------------------------------------------------------------------------


def test_a_client_receives_a_disclosure_and_never_the_stored_entry(w):
    both = _fact(w)
    both.extra_categories = ["coordination_context"]
    both.save(update_fields=["extra_categories"])
    body = _q(w).json()
    d = body["entries"][0]
    assert set(d) == {"ref", "version", "content", "evidence", "assertion", "classification", "standing", "useUntil"}
    assert d["content"]["value"] == "KC metrics lead." and d["standing"] == {"assurance": "authenticated",
                                                                              "confirmed": False}
    # Only the categories the grant covers are named (it covers all four here, so both) ...
    assert set(d["classification"]["categories"]) == {"work_context", "coordination_context"}
    text = json.dumps(body)
    assert str(both.entry_id) not in text and "subject" not in text and "provenance" not in text
    assert "directives" in body and body["minimization"] == {"method": hcp.minimization(hcp.agent_actor(w["ada"]))["method"]}
    assert dt.datetime.fromisoformat(d["useUntil"]) > timezone.now()


def test_a_ref_is_stable_for_one_grant_and_means_nothing_under_another(w):
    fact = _fact(w)
    other = PersonGrant.objects.create(person=w["person"], workspace=w["ws"], agent=w["ada"], client_key="other",
                                       client_name="Other", scopes=w["grant"].scopes, grant_type="persistent")
    ref = disclosure.ref_for(w["grant"], fact.entry_id)
    assert ref == disclosure.ref_for(w["grant"], fact.entry_id) != disclosure.ref_for(other, fact.entry_id)
    assert str(fact.entry_id) not in ref
    got = _as(w["ada"].user).get(f"/api/hcp/v2/entries/{ref}?purpose={purposes.ASSISTANCE}&turn={w['turn'].pk}")
    assert got.status_code == 200 and got.json()["entry"]["ref"] == ref
    assert _as(w["ada"].user).get(f"/api/hcp/v2/entries/{disclosure.ref_for(other, fact.entry_id)}"
                                  f"?turn={w['turn'].pk}").status_code == 403
    assert _as(w["ada"].user).get(f"/api/hcp/v2/entries/urn:uuid:{fact.entry_id}?turn={w['turn'].pk}").status_code == 403


def test_a_relationship_is_disclosed_only_where_its_target_is_too(w):
    a = _fact(w, "Leads KC.")
    b = _fact(w, "Runs the KC dashboards.")
    hcp.relationships._record(w["person"], b.entry_id, "derivedFrom", role="context-client", by="agent:ada",
                              target_entry_id=a.entry_id)
    refs = {x.entry_id: disclosure.ref_for(w["grant"], x.entry_id) for x in (a, b)}
    both = disclosure.to_disclosure(b, w["grant"], refs=refs)
    assert both["relationships"] == [{"type": "derivedFrom", "target": refs[a.entry_id]}]
    assert "relationships" not in disclosure.to_disclosure(b, w["grant"], refs={b.entry_id: refs[b.entry_id]})


def test_the_person_reading_through_v2_gets_the_whole_entry(w):
    f = _fact(w)
    c = _as(w["lili"])
    one = c.get(f"/api/hcp/v2/entries/urn:uuid:{f.entry_id}").json()["entry"]
    assert one["id"] == f"urn:uuid:{f.entry_id}" and one["record"]["status"] == "active" and one["schema"] == v2.ENTRY_SCHEMA
    q = _j(c, "post", "/api/hcp/v2/entries/query", {"query": "KC", "purpose": "review", "categories": ["work_context"]})
    assert q.status_code == 200 and q.json()["entries"][0]["id"] == one["id"]
    assert q["HCP-Version"] == "2.0"


# --- v2 writes (5.2.3-5.2.5) ---------------------------------------------------------------------


def _entry_body(w, value="Prefers short replies.", declaration="model-inferred", **assertion):
    return {"schema": v2.ENTRY_SCHEMA, "subject": {"id": hcp.subject_id(w["person"])},
            "content": {"profile": v2.GENERAL_CONTEXT, "value": value},
            "classification": {"categories": ["general_preferences", "work_context"], "dimension": "length"},
            "assertion": {"declarationType": declaration, "confidence": "medium", **assertion},
            "record": {"provenance": {"source": f"turn:{w['turn'].pk}"}}}


def test_an_agent_creates_revises_and_withdraws_through_v2(w):
    c = _as(w["ada"].user)
    t = f"turn={w['turn'].pk}"
    body = _entry_body(w, basis={"drawnFrom": ["conversation"], "rationale": "they said so twice"},
                       permittedPurposes=[purposes.ASSISTANCE])
    body["record"]["relationships"] = [{"type": "derivedFrom", "target": "urn:canopy:source:session:42"}]
    r = _j(c, "post", f"/api/hcp/v2/entries?{t}", body)
    assert r.status_code == 201, r.content
    made = r.json()
    fact = PersonFact.objects.get(statement="Prefers short replies.")
    assert (fact.category, fact.extra_categories, fact.basis, fact.assurance) == (
        "general_preferences", ["work_context"], "inferred", "unverified")
    assert fact.basis_detail["drawnFrom"] == ["conversation"] and fact.permitted_purposes == [purposes.ASSISTANCE]
    assert made["assertion"]["declarationType"] == "model-inferred"
    # The person sees the relationship to its source, in v2's own shape.
    mine = _as(w["lili"]).get(f"/api/hcp/v2/entries/urn:uuid:{fact.entry_id}").json()["entry"]
    assert mine["record"]["relationships"][0]["target"] == "urn:canopy:source:session:42"
    assert mine["assertion"]["basis"]["rationale"] == "they said so twice"
    # Revise: stale version conflicts, current succeeds, and the ref survives.
    ref = made["ref"]
    stale = _j(c, "post", f"/api/hcp/v2/entries/{ref}/revisions?{t}",
               {"expectedVersion": 7, "content": {"profile": v2.GENERAL_CONTEXT, "value": "x y z"}, "reason": "r"})
    assert stale.status_code == 409 and stale.json()["type"].endswith("version-conflict")
    ok = _j(c, "post", f"/api/hcp/v2/entries/{ref}/revisions?{t}",
            {"expectedVersion": 1, "content": {"profile": v2.GENERAL_CONTEXT, "value": "Prefers brief replies."},
             "reason": "narrower"})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["ref"] == ref
    # Withdraw: hidden from the client at once, held for the person; no hard delete for an agent.
    assert c.delete(f"/api/hcp/v2/entries/{ref}?{t}&hardDelete=true").status_code == 403
    assert c.delete(f"/api/hcp/v2/entries/{ref}?{t}&reason=gone").status_code == 204
    assert PersonFact.objects.get(entry_id=fact.entry_id, superseded_at__isnull=True).status == "deleted"
    assert c.get(f"/api/hcp/v2/entries/{ref}?{t}").status_code == 403


def test_v2_create_refuses_what_the_spec_does(w):
    c, t = _as(w["ada"].user), f"turn={w['turn'].pk}"
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", {**_entry_body(w), "schema": "https://x/y"}).status_code == 422
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", _entry_body(w, declaration="issuer-attested")).status_code == 403
    no_conf = _entry_body(w)
    no_conf["assertion"]["confidence"] = None
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", no_conf).status_code == 422
    other_subject = {**_entry_body(w), "subject": {"id": "did:hcp:user:someone-else"}}
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", other_subject).status_code == 422
    bad_basis = _entry_body(w, basis={"conversationId": "abc"})
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", bad_basis).status_code == 422
    outside = _entry_body(w)
    outside["classification"]["categories"] = ["health_context"]
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", outside).status_code == 422
    assert not PersonFact.objects.filter(statement="Prefers short replies.").exists()


def test_an_agents_declared_entry_through_v2_is_unverified_and_the_persons_is_authenticated(w):
    c, t = _as(w["ada"].user), f"turn={w['turn'].pk}"
    _j(c, "post", f"/api/hcp/v2/entries?{t}", _entry_body(w, "They like tables.", declaration="person-declared",
                                                           confidence=None))
    mine = _j(_as(w["lili"]), "post", "/api/hcp/v2/entries", _entry_body(w, "I like diagrams.",
              declaration="person-declared", confidence=None))
    assert mine.status_code == 201, mine.content
    got = {f.statement: f.assurance for f in PersonFact.objects.all()}
    assert got == {"They like tables.": "unverified", "I like diagrams.": "authenticated"}


# --- the audit, the summary and the rest of the governance plane -----------------------------------


def test_the_v2_audit_names_what_each_read_disclosed(w):
    f = _fact(w)
    _q(w)
    page = _as(w["lili"]).get("/api/hcp/v2/audit").json()
    read = next(e for e in page["events"] if e["eventType"] == "context.read")
    assert read["purpose"] == purposes.ASSISTANCE and read["entryId"] == f"urn:uuid:{f.entry_id}"
    assert read["disclosed"][0]["version"] == 1 and read["disclosed"][0]["useUntil"]
    assert read["participants"]["initiator"]["role"] == "context-client" and read["participants"]["retrievalId"]
    assert _as(w["ada"].user).get("/api/hcp/v2/audit").status_code == 403


def test_the_summary_matches_the_raw_events(w):
    a, b = _fact(w, "Leads KC."), _fact(w, "Runs dashboards.")
    _q(w)
    _q(w, categories=["work_context"])
    summary = _as(w["lili"]).get("/api/hcp/v2/disclosures").json()["clients"]
    assert len(summary) == 1
    s = summary[0]
    raw = PersonAuditEvent.objects.filter(person=w["person"], event_type="preference.read")
    assert s["client"]["id"] == "agent:ada" and s["client"]["operator"]["id"] == "canopy.dimagi.com"
    assert s["entries"] == len({e.entry_id for e in raw if e.entry_id}) == 2
    assert s["retrievals"] == len({e.data["retrievalId"] for e in raw}) == 2
    assert s["categories"] == ["work_context"] and s["purposes"] == [purposes.ASSISTANCE]
    assert s["grants"] == [{"grantId": f"urn:uuid:{w['grant'].grant_id}", "status": "active"}]
    assert s["notification"] == "none"
    assert s["usableUntil"] == max(e.data["useUntil"] for e in raw)
    assert _as(w["lili"]).get("/api/hcp/v2/disclosures?client=agent:nobody").json() == {"clients": []}
    assert _as(w["ada"].user).get("/api/hcp/v2/disclosures").status_code == 403
    assert a and b


def test_a_delivered_revocation_notice_ends_the_copies_in_the_summary(w):
    from apps.contacts.models import HcpRevocationDelivery

    client = HcpClient.objects.create(client_id="c", name="C", operator="O", redirect_uris=["https://c.o.example/cb"],
                                      webhook_url="https://c.o.example/hook",
                                      legal_retention={"record": "case file", "obligation": "law", "period": "7y"})
    grant = PersonGrant.objects.create(person=w["person"], hcp_client=client, client_key="c", client_name="C",
                                       scopes=["hcp:work_context:read"], grant_type="persistent")
    f = _fact(w)
    PersonAuditEvent.objects.create(
        person=w["person"], event_type="preference.read", actor_id="client:c", actor_type="agent", entry_id=f.entry_id,
        category="work_context", purpose_code=purposes.ASSISTANCE, grant_id=grant.grant_id,
        data={"retrievalId": "urn:uuid:1", "useUntil": (timezone.now() + dt.timedelta(days=3)).isoformat()})
    when = timezone.now()
    HcpRevocationDelivery.objects.create(grant=grant, client=client, payload={}, next_attempt_at=when,
                                         delivered_at=when)
    s = [c for c in _as(w["lili"]).get("/api/hcp/v2/disclosures").json()["clients"] if c["client"]["id"] == "c"][0]
    assert s["notification"] == "delivered" and s["usableUntil"] == when.isoformat()
    assert s["legalRetention"]["record"] == "case file"


def test_grants_are_listed_by_the_person_and_a_client_sees_only_its_own(w):
    mine = _as(w["lili"]).get("/api/hcp/v2/grants").json()["grants"][0]
    assert mine["access"][0]["purposes"] and set(mine["access"][0]["actions"]) == {"read", "write"}
    assert mine["client"]["operator"]["id"] == "canopy.dimagi.com" and mine["authorizingPrincipal"]["id"]
    PersonGrant.objects.create(person=w["person"], agent=w["ada"], workspace=w["ws"], client_key="z", client_name="Z",
                               scopes=["hcp:work_context:read"], grant_type="persistent")
    theirs = _as(w["ada"].user).get(f"/api/hcp/v2/grants?turn={w['turn'].pk}").json()["grants"]
    assert [g["grantId"] for g in theirs] == [f"urn:uuid:{w['grant'].grant_id}"]


def test_the_person_revokes_and_a_client_may_only_relinquish_its_own_grant(w):
    other = PersonGrant.objects.create(person=w["person"], agent=w["ada"], workspace=w["ws"], client_key="z",
                                       client_name="Z", scopes=["hcp:work_context:read"], grant_type="persistent")
    ada = _as(w["ada"].user)
    assert ada.delete(f"/api/hcp/v2/grants/urn:uuid:{other.grant_id}?turn={w['turn'].pk}").status_code == 403
    assert _as(w["lili"]).delete(f"/api/hcp/v2/grants/urn:uuid:{other.grant_id}").status_code == 204
    other.refresh_from_db()
    assert other.status == "revoked"
    assert ada.delete(f"/api/hcp/v2/grants/urn:uuid:{w['grant'].grant_id}?turn={w['turn'].pk}").status_code == 204
    w["grant"].refresh_from_db()
    assert w["grant"].status == "revoked"


def test_export_carries_entries_rules_and_directives_in_v2_form(w):
    _fact(w)
    c = _as(w["lili"])
    _j(c, "post", "/api/hcp/v2/rules", {"operators": ["x.example"]})
    out = c.get("/api/hcp/v2/export?include=audit").json()
    assert out["entries"][0]["schema"] == v2.ENTRY_SCHEMA and out["rules"][0]["operators"] == ["x.example"]
    assert out["audit"] and _as(w["ada"].user).get("/api/hcp/v2/export").status_code == 403


# --- discovery advertises both (19) ----------------------------------------------------------------


def test_discovery_serves_v1_by_default_and_v2_to_a_client_that_asks_and_each_points_at_the_other(w):
    c = Client()
    v1 = c.get("/api/hcp/.well-known/hcp-configuration").json()
    assert v1["hcp_version"] == "1.0" and v1["conformance_level"] == "HCP-v1-Core"
    assert "HCP-Version: 2.0" in v1["canopy"]["v2"]["discovery"]
    r = c.get("/api/hcp/.well-known/hcp-configuration", HTTP_HCP_VERSION="2.0")
    v2doc = r.json()
    assert r["HCP-Version"] == "2.0" and v2doc["hcp_version"] == "2.0"
    assert v2doc["conformsTo"] == [] and "conformance_level" not in v2doc          # claims no v2 identifier yet
    assert v2doc["entries_endpoint"].endswith("/api/hcp/v2/entries") and v2doc["bindings"] == ["http"]
    assert v2doc["conflict_detection"] == "structural" and v2doc["accepts_entry_schemas"] == [v2.ENTRY_SCHEMA]
    assert {x["id"] for x in v2doc["supported_categories"]} >= set(hcp.HELD_CATEGORIES)
    assert c.get("/.well-known/hcp-configuration", HTTP_HCP_VERSION="2.0").json()["hcp_version"] == "2.0"
    assert c.get("/api/hcp/.well-known/hcp-configuration", HTTP_HCP_VERSION="3.0").status_code == 400


def test_v1_reads_still_carry_use_until_as_metadata_and_the_v1_wire_is_unchanged(w):
    _fact(w)
    r = _j(_as(w["ada"].user), "post", f"/api/hcp/v1/preferences/search?turn={w['turn'].pk}",
           {"query": "KC metrics", "categories": ["work_context"], "purpose": "answer"}).json()
    entry = r["entries"][0]
    assert entry["record"]["metadata"]["canopy:useUntil"] and entry["claim"]["subject"]["preference"]
    assert r["hcp_version"] == "1.0" and r["directives"] == []


# --- a revision keeps what the entry carried; standing is the person's alone -------------------------


def _v2_entry(w, **assertion):
    r = _j(_as(w["ada"].user), "post", f"/api/hcp/v2/entries?turn={w['turn'].pk}",
           _entry_body(w, basis={"drawnFrom": ["conversation"]}, **assertion))
    assert r.status_code == 201, r.content
    return PersonFact.objects.get(statement="Prefers short replies.", superseded_at__isnull=True)


def test_a_revision_keeps_the_entrys_other_categories_and_basis_on_every_path(w):
    fact = _v2_entry(w)
    assert fact.extra_categories == ["work_context"]
    # A v1 revision by an agent (hcp_updatePreference) ...
    r = _as(w["ada"].user).put(f"/api/hcp/v1/preferences/urn:uuid:{fact.entry_id}?turn={w['turn'].pk}",
                               data=json.dumps({"updatedPreference": "Prefers brief replies.", "reason": "r"}),
                               content_type="application/json")
    assert r.status_code == 200, r.content
    new = hcp.current(fact.entry_id)
    assert (new.version, new.extra_categories, new.basis_detail) == (2, ["work_context"], {"drawnFrom": ["conversation"]})
    # ... and the people route's supersede keep both, so a rule denying work_context still withholds it.
    r = _j(_as(w["ada"].user), "post", f"/api/people/{w['person'].pk}/facts/",
           {"workspace": "connect", "kind": "preference", "statement": "Prefers very brief replies.",
            "basis": "inferred", "confidence": "low", "supersedes_id": new.pk, "category": "general_preferences",
            "source_turn_id": str(w["turn"].pk)})
    assert r.status_code == 201, r.content
    newest = hcp.current(fact.entry_id)
    assert newest.extra_categories == ["work_context"] and newest.basis_detail == {"drawnFrom": ["conversation"]}
    assert disclosure.sensitivity_of(hcp.core.categories_of(newest)) == "medium"


def test_superseding_through_the_people_route_keeps_the_purposes(w):
    f = _fact(w, permitted_purposes=[purposes.ASSISTANCE, purposes.COACHING])
    hcp.restrict_purposes(f, [purposes.ASSISTANCE], actor=hcp.user_actor(w["lili"]))
    r = _j(_as(w["ada"].user), "post", f"/api/people/{w['person'].pk}/facts/",
           {"workspace": "connect", "kind": "role", "statement": "Leads KC metrics.", "supersedes_id": f.pk,
            "source_turn_id": str(w["turn"].pk)})
    assert r.status_code == 201, r.content
    new = hcp.current(f.entry_id)
    assert new.version == 2
    assert new.permitted_purposes == [purposes.ASSISTANCE, purposes.COACHING]
    assert new.restricted_purposes == [purposes.ASSISTANCE]


def test_only_the_persons_own_write_is_authenticated(w):
    jj = User.objects.get(username="jj")            # a workspace owner, not the subject
    r = _j(_as(jj), "post", f"/api/people/{w['person'].pk}/facts/",
           {"workspace": "connect", "kind": "role", "statement": "Runs the KC pilot."})
    assert r.status_code == 201, r.content
    assert PersonFact.objects.get(statement="Runs the KC pilot.").assurance == "unverified"
    r = _j(_as(w["lili"]), "post", f"/api/people/{w['person'].pk}/facts/",
           {"workspace": "connect", "kind": "role", "statement": "Owns the KC dashboard."})
    assert r.status_code == 201, r.content
    assert PersonFact.objects.get(statement="Owns the KC dashboard.").assurance == "authenticated"


# --- governance is the person's browser session or hand-minted PAT -----------------------------------


def _oauth_client_token(user):
    from apps.oauth_server.models import OAuthClient, OAuthGrant
    from apps.tokens.models import PersonalToken

    client = OAuthClient.objects.create(client_id="mcp-1", client_name="Some MCP client", redirect_uris=[])
    grant = OAuthGrant.objects.create(client=client, user=user)
    raw, _ = PersonalToken.create_for_user(user=user, label="oauth", ttl=dt.timedelta(hours=1), oauth_grant=grant)
    return raw


def test_an_oauth_sign_in_cannot_govern_even_as_the_person(w):
    from tests.token_helpers import bearer_client, pat_client

    f = _fact(w)
    entry = f"/api/hcp/v2/entries/urn:uuid:{f.entry_id}"
    for c, allowed in ((bearer_client(_oauth_client_token(w["lili"])), False), (pat_client(w["lili"]), True)):
        want = 200 if allowed else 403
        assert c.get("/api/hcp/v1/audit").status_code == want
        assert c.get("/api/hcp/v1/export").status_code == want
        assert c.get("/api/hcp/v2/export").status_code == want
        assert _j(c, "post", "/api/hcp/v2/rules", {"operators": ["x.example"]}).status_code == (201 if allowed else 403)
        assert _j(c, "post", f"{entry}/confirmation", {"disposition": "confirmed"}).status_code == want
    from apps.contacts.models import StandingRule

    assert StandingRule.objects.count() == 1                          # the PAT's, and only it


# --- what discovery says about OAuth clients ---------------------------------------------------------


def test_v2_discovery_does_not_offer_v2_to_oauth_clients(w):
    doc = Client().get("/api/hcp/.well-known/hcp-configuration", HTTP_HCP_VERSION="2.0").json()
    assert "/api/hcp/v1/" in doc["canopy"]["oauth_clients"] and "not served" in doc["canopy"]["oauth_clients"]
    assert "v2 for OAuth clients of the HCP service" in doc["canopy"]["not_built"]
    v1doc = Client().get("/api/hcp/.well-known/hcp-configuration").json()
    assert "/api/hcp/v1/" in v1doc["canopy"]["v2"]["oauth_clients"]


# --- the disclosure summary, per grant ---------------------------------------------------------------


def test_a_revocation_notice_ends_only_its_own_grants_copies_in_the_summary(w):
    from apps.contacts.models import HcpRevocationDelivery

    client = HcpClient.objects.create(client_id="c", name="C", operator="O", redirect_uris=["https://c.o.example/cb"],
                                      webhook_url="https://c.o.example/hook")
    old, live = (PersonGrant.objects.create(person=w["person"], hcp_client=client, client_key=k, client_name="C",
                                            scopes=["hcp:work_context:read"], grant_type="persistent")
                 for k in ("c1", "c2"))
    f = _fact(w)
    now = timezone.now()
    for grant, days in ((old, 3), (live, 2)):
        PersonAuditEvent.objects.create(
            person=w["person"], event_type="preference.read", actor_id="client:c", actor_type="agent",
            entry_id=f.entry_id, category="work_context", purpose_code=purposes.ASSISTANCE, grant_id=grant.grant_id,
            data={"retrievalId": f"urn:uuid:{days}", "useUntil": (now + dt.timedelta(days=days)).isoformat()})
    HcpRevocationDelivery.objects.create(grant=old, client=client, payload={}, next_attempt_at=now, delivered_at=now)
    s = [c for c in _as(w["lili"]).get("/api/hcp/v2/disclosures").json()["clients"] if c["client"]["id"] == "c"][0]
    # The revoked grant's copies ended at the notice; the live grant's are usable for two more days.
    assert s["usableUntil"] == (now + dt.timedelta(days=2)).isoformat()


def test_an_empty_search_names_no_category_and_records_what_was_searched(w):
    _q(w, query="nothing like anything held", categories=["work_context", "general_preferences"])
    e = PersonAuditEvent.objects.get(person=w["person"], event_type="preference.read")
    assert e.entry_id is None and e.category == ""
    assert e.data["categoriesSearched"] == ["work_context", "general_preferences"]
    assert v2.event_v2(e)["categories"] == []


# --- a relationship that changes what others are served takes write on its target --------------------


def test_contradicting_or_superseding_an_entry_takes_write_on_its_category(w):
    target = _fact(w, "Prefers long replies.", category="work_context", dimension="length")
    g = w["grant"]
    g.scopes = ["hcp:work_context:read", "hcp:general_preferences:read", "hcp:general_preferences:write"]
    g.save(update_fields=["scopes"])
    c, t = _as(w["ada"].user), f"turn={w['turn'].pk}"
    ref = _q(w, query="long replies", categories=["work_context"]).json()["entries"][0]["ref"]
    for kind in ("contradicts", "supersedes"):
        body = _entry_body(w, value=f"Prefers short replies ({kind}).")
        body["classification"]["categories"] = ["general_preferences"]
        body["record"]["relationships"] = [{"type": kind, "target": ref}]
        r = _j(c, "post", f"/api/hcp/v2/entries?{t}", body)
        assert r.status_code == 403, r.content
    target.refresh_from_db()
    assert target.status == "active"
    assert not PersonFact.objects.filter(statement__startswith="Prefers short replies (").exists()
    # derivedFrom only needs read.
    body = _entry_body(w, value="Prefers short replies (derived).")
    body["classification"]["categories"] = ["general_preferences"]
    body["record"]["relationships"] = [{"type": "derivedFrom", "target": ref}]
    assert _j(c, "post", f"/api/hcp/v2/entries?{t}", body).status_code == 201


def test_resolving_a_ref_computes_the_grants_reach_once_not_per_entry(w, monkeypatch):
    from apps.contacts.hcp import grants as grants_mod

    for n in range(6):
        _fact(w, f"Leads KC workstream {n}.")
    ref = _q(w).json()["entries"][0]["ref"]
    calls = []
    real = grants_mod.reach_of

    def counted(grant):
        calls.append(grant.pk)
        return real(grant)
    monkeypatch.setattr(grants_mod, "reach_of", counted)
    monkeypatch.setattr(hcp, "reach_of", counted)
    r = _as(w["ada"].user).get(f"/api/hcp/v2/entries/{ref}?turn={w['turn'].pk}&purpose={purposes.ASSISTANCE}")
    assert r.status_code == 200, r.content
    assert len(calls) <= 2                     # the principal's checks, then once for the lookup
