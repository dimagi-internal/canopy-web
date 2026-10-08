"""Human Context Protocol v1 (draft 4) over the people brain — apps/contacts/hcp.py.

Each test pins a MUST of the spec, cited by section, or one of canopy's own
rules on top of it:

* an agent reaches ONLY the person who started the turn it names (caller-only),
  and only under that client's grant;
* a grant is per client = (agent, channel, host), presumed by the control
  plane once, and never re-presumed after the person revokes it.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.models import PersonAuditEvent, PersonFact, PersonGrant
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "/api/hcp"


def _member(ws, username, **kw):
    u = User.objects.create_user(username, f"{username}@dimagi.com", "pw", **kw)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return u


def _agent(ws, slug, owner):
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
    u = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = u
    agent.save(update_fields=["user"])
    return agent


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = _member(ws, "lili", first_name="Lilianna")
    bob = _member(ws, "bob")
    ace, hal = _agent(ws, "ace", owner), _agent(ws, "hal", owner)
    return {"ws": ws, "lili": lili, "bob": bob, "ace": ace, "hal": hal, "owner": owner,
            "person": contacts.person_for(user=lili)}


def _turn(agent, user, key, via="chat", prompt="hi"):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=prompt,
        initiator=who.for_user(user, via=via, assurance=who.SESSION))
    return turn


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(c, path, body, **headers):
    return c.post(BASE + path, data=json.dumps(body), content_type="application/json",
                  **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()})


def _add(person, ws, statement, *, category="work_context", basis="declared", dimension="",
         kind="role", confidence=None):
    return people.record_fact(person=person, workspace=ws, kind=kind, statement=statement,
                              basis=basis, category=category, dimension=dimension,
                              confidence=confidence)


def _events(person, kind):
    return list(PersonAuditEvent.objects.filter(person=person, event_type=kind).order_by("pk"))


def _search(c, turn, **body):
    body = {"query": "kangaroo care coach", "categories": ["work_context"],
            "purpose": "answer the question", **body}
    return _post(c, f"/v1/preferences/search?turn={turn.pk}", body)


# --- data model ----------------------------------------------------------------


def test_an_update_is_a_new_version_of_the_same_entry(w):
    f1 = _add(w["person"], w["ws"], "KC metrics lead.")
    f2 = hcp.update_entry(f1, statement="KC and KMC metrics lead.", reason="correction",
                          actor=hcp.user_actor(w["lili"]), by_user=w["lili"])
    assert f2.entry_id == f1.entry_id and (f1.version, f2.version) == (1, 2)       # 2.2
    doc = hcp.to_entry(f2, redact=False)
    assert doc["id"] == f"urn:uuid:{f1.entry_id}"
    assert (doc["record"]["version"], doc["record"]["previousVersion"]) == (2, 1)
    assert doc["@context"] == hcp.CONTEXT and doc["credentialType"] == "NLPreference"
    assert [e.event_type for e in PersonAuditEvent.objects.filter(entry_id=f1.entry_id)
            .order_by("pk")] == ["preference.created", "preference.updated"]


def test_confidence_is_required_for_an_inference_and_forbidden_otherwise(w):
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t")
    base = {"category": "work_context", "preference": "Works on KC.", "sourceContext": "turn:x"}
    r = _post(c, f"/v1/preferences/add?turn={turn.pk}", {**base, "declarationType": "model-inferred"})
    assert r.status_code == 422 and r.json()["type"] == hcp.PROBLEM_BASE + "malformed-request"
    r = _post(c, f"/v1/preferences/add?turn={turn.pk}",
              {**base, "declarationType": "user-declared", "confidence": "high"})
    assert r.status_code == 422
    r = _post(c, f"/v1/preferences/add?turn={turn.pk}",
              {**base, "declarationType": "model-inferred", "confidence": "high"})
    assert r.status_code == 201
    rec = r.json()["entry"]["record"]
    assert (rec["declarationType"], rec["confidence"]) == ("model-inferred", "high")


# --- conflicts (2.6) ---------------------------------------------------------


@pytest.mark.parametrize("declared_first", [True, False])
def test_an_inference_contradicting_a_declaration_is_quarantined_either_order(w, declared_first):
    p, ws = w["person"], w["ws"]
    if declared_first:
        d = _add(p, ws, "Prefers email.", category="general_preferences", dimension="contact channel")
        i = _add(p, ws, "Prefers Slack.", category="general_preferences", basis="inferred",
                 dimension="Contact  Channel", confidence="low")             # normalized: matches
    else:
        i = _add(p, ws, "Prefers Slack.", category="general_preferences", basis="inferred",
                 dimension="contact_channel", confidence="low")
        d = _add(p, ws, "Prefers email.", category="general_preferences", dimension="contact channel")
    i.refresh_from_db(), d.refresh_from_db()
    assert (i.status, d.status) == ("conflicted", "active")
    ev = _events(p, "conflict.detected")
    assert len(ev) == 1 and (ev[0].entry_id, ev[0].related_entry_id) == (i.entry_id, d.entry_id)
    # Withheld from agents (2.6.2), visible to the person.
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t", prompt="slack or email?")
    got = _search(c, turn, query="slack email", categories=["general_preferences"]).json()
    assert [e["claim"]["subject"]["preference"] for e in got["entries"]] == ["Prefers email."]
    assert _as(w["lili"]).get(f"{BASE}/v1/preferences/urn:uuid:{i.entry_id}?purpose=review").status_code == 200


def test_no_dimension_no_automatic_conflict_and_never_on_a_declared_entry(w):
    p, ws = w["person"], w["ws"]
    _add(p, ws, "Prefers email.", category="general_preferences")
    i = _add(p, ws, "Prefers Slack.", category="general_preferences", basis="inferred", confidence="low")
    assert i.status == "active" and not _events(p, "conflict.detected")


def test_the_person_resolves_a_conflict_by_accepting_or_rejecting(w):
    p, ws, me = w["person"], w["ws"], hcp.user_actor(w["lili"])
    d = _add(p, ws, "Prefers email.", category="general_preferences", dimension="channel")
    i = _add(p, ws, "Prefers Slack.", category="general_preferences", basis="inferred",
             dimension="channel", confidence="low")
    i.refresh_from_db()
    accepted = hcp.update_entry(i, statement="Prefers Slack.", reason="that's right", actor=me,
                                by_user=w["lili"])
    d.refresh_from_db()
    assert (accepted.status, accepted.basis, d.status) == ("active", "declared", "deprecated")
    assert _events(p, "conflict.resolved")
    # Rejecting: a new inference on the now-declared dimension, deleted by the person.
    j = _add(p, ws, "Prefers Teams.", category="general_preferences", basis="inferred",
             dimension="channel", confidence="low")
    j.refresh_from_db()
    assert j.status == "conflicted"
    hcp.delete_entry(j, actor=me, reason="wrong", by_user=w["lili"])
    j.refresh_from_db()
    assert j.status == "deleted" and len(_events(p, "conflict.resolved")) == 2


# --- grants (4.1) ------------------------------------------------------------


def test_the_envelope_presumes_one_grant_per_client_and_audits_it(w):
    p = w["person"]
    caller_context.build(_turn(w["ace"], w["lili"], "a"))
    caller_context.build(_turn(w["ace"], w["lili"], "b"))                   # same client
    caller_context.build(_turn(w["ace"], w["lili"], "c", via="slack"))      # another channel
    grants = PersonGrant.objects.filter(person=p).order_by("pk")
    assert [(g.agent.slug, g.channel, g.grant_type, g.modality) for g in grants] == [
        ("ace", "chat", "persistent", "canopy-control-plane"),
        ("ace", "slack", "persistent", "canopy-control-plane")]
    issued = _events(p, "grant.issued")
    assert len(issued) == 2 and "presumed by the canopy control plane" in issued[0].detail
    assert "scopes=hcp:general_preferences:read" in issued[0].detail


def test_a_revoked_client_is_never_re_presumed_and_other_clients_are_unaffected(w):
    p = w["person"]
    _add(p, w["ws"], "KC metrics lead.")
    caller_context.build(_turn(w["ace"], w["lili"], "a"))
    slack = _turn(w["ace"], w["lili"], "s", via="slack")
    caller_context.build(slack)
    chat = PersonGrant.objects.get(person=p, channel="chat")
    r = _as(w["lili"]).delete(f"{BASE}/v1/grants/urn:uuid:{chat.grant_id}")
    assert r.status_code == 200 and r.json()["grant"]["status"] == "revoked"
    assert _events(p, "grant.revoked")
    env = caller_context.build(_turn(w["ace"], w["lili"], "b"))
    assert env["person"]["facts"] == [] and env["person"]["grant"] is None
    assert PersonGrant.objects.filter(person=p, channel="chat").count() == 1   # not re-presumed
    later = _turn(w["ace"], w["lili"], "b2")
    assert _search(_as(w["ace"].user), later).status_code == 403
    # Slack is a different client: still served.
    assert _search(_as(w["ace"].user), slack).json()["entries"]


def test_an_agent_lists_only_its_own_grant_and_cannot_revoke(w):
    caller_context.build(_turn(w["hal"], w["lili"], "h"))
    turn = _turn(w["ace"], w["lili"], "a")
    got = _as(w["ace"].user).get(f"{BASE}/v1/grants?turn={turn.pk}").json()
    assert [g["canopy"]["agent"] for g in got["grants"]] == ["ace"]
    hal_grant = PersonGrant.objects.get(agent=w["hal"])
    assert _as(w["ace"].user).delete(f"{BASE}/v1/grants/{hal_grant.grant_id}").status_code == 403


# --- the caller-only subject rule -----------------------------------------------


def test_an_agent_reaches_only_the_person_who_started_its_turn(w):
    _add(w["person"], w["ws"], "KC metrics lead.")
    bobs = contacts.person_for(user=w["bob"])
    _add(bobs, w["ws"], "Bob runs finance.")
    c = _as(w["ace"].user)
    lili_turn = _turn(w["ace"], w["lili"], "l")
    got = _search(c, lili_turn, query="metrics finance").json()
    assert [e["claim"]["subject"]["preference"] for e in got["entries"]] == ["KC metrics lead."]
    # Another agent's turn: refused, and the answer is the same 403 as "missing".
    hal_turn = _turn(w["hal"], w["bob"], "h")
    r = _search(c, hal_turn)
    assert r.status_code == 403 and r.json()["type"] == hcp.PROBLEM_BASE + "scope-denied"
    # No turn at all: the agent must say whom it serves.
    assert _post(c, "/v1/preferences/search", {"query": "x", "categories": ["work_context"],
                                               "purpose": "p"}).status_code == 422


def test_the_legacy_people_read_refuses_an_agent_login(w):
    r = _as(w["ace"].user).get(f"/api/people/{w['person'].pk}/?workspace=connect")
    assert r.status_code == 403


# --- minimization (4.4) -------------------------------------------------------


def test_search_is_scoped_bounded_unpadded_ordered_and_redacted(w):
    p = w["person"]
    for n in range(25):
        _add(p, w["ws"], f"Works on kangaroo care stream {n}.", kind="instance")
    _add(p, w["ws"], "Unrelated: owns the finance dashboard.", kind="instance")
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t")
    body = _search(c, turn, maxEntries=50).json()
    assert len(body["entries"]) == 20                                         # 4.4.2 ceiling
    assert all("kangaroo" in e["claim"]["subject"]["preference"] for e in body["entries"])
    assert len(_search(c, turn, query="dashboard finance", maxEntries=5).json()["entries"]) == 1  # not padded
    prov = body["entries"][0]["record"]["provenance"]
    assert "source" not in prov and "capturedBy" not in prov                   # 4.4.4
    assert body["minimization"] == {"method": "lexical", "redactedFields": hcp.REDACTED_FIELDS}
    assert body["hcp_version"] == "1.0"
    assert _search(c, turn, categories=["health_context"]).status_code == 403   # 4.4.1
    mine = _post(_as(w["lili"]), "/v1/preferences/search",
                 {"query": "finance", "categories": ["work_context"], "purpose": "me"}).json()
    assert mine["minimization"]["redactedFields"] == []
    assert "source" in mine["entries"][0]["record"]["provenance"]
    reads = _events(p, "preference.read")
    assert reads and all(e.purpose for e in reads)


def test_minimal_response_detail(w):
    _add(w["person"], w["ws"], "KC metrics lead.")
    body = _search(_as(w["ace"].user), _turn(w["ace"], w["lili"], "t"), query="metrics",
                   responseDetail="minimal").json()
    assert body["entries"] == [{"id": body["entries"][0]["id"],
                                "record": {"category": "work_context", "dimension": None},
                                "value": "KC metrics lead."}]


# --- writes, deletes, idempotency -------------------------------------------------


def test_only_the_person_moves_a_category_or_hard_deletes(w):
    f = _add(w["person"], w["ws"], "KC metrics lead.")
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t")
    url = f"{BASE}/v1/preferences/urn:uuid:{f.entry_id}?turn={turn.pk}"
    r = c.put(url, data=json.dumps({"updatedPreference": "x", "reason": "r",
                                    "category": "general_preferences"}),
              content_type="application/json")
    assert r.status_code == 403
    assert c.delete(url + "&hardDelete=true&reason=r").status_code == 403
    r = _as(w["lili"]).put(f"{BASE}/v1/preferences/urn:uuid:{f.entry_id}",
                           data=json.dumps({"updatedPreference": "KC metrics lead.", "reason": "move",
                                            "category": "general_preferences"}),
                           content_type="application/json")
    assert r.status_code == 200
    assert "category work_context -> general_preferences" in _events(w["person"], "preference.updated")[-1].detail
    r = _as(w["lili"]).delete(f"{BASE}/v1/preferences/urn:uuid:{f.entry_id}?hardDelete=true&reason=gdpr")
    assert r.status_code == 200 and not PersonFact.objects.filter(entry_id=f.entry_id).exists()
    ev = _events(w["person"], "preference.hardDeleted")
    assert len(ev) == 1 and ev[0].entry_id == f.entry_id


def test_a_replayed_write_is_not_applied_or_audited_twice(w):
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t")
    body = {"category": "work_context", "preference": "Works on KC.", "declarationType": "user-declared",
            "sourceContext": f"turn:{turn.pk}"}
    r1 = _post(c, f"/v1/preferences/add?turn={turn.pk}", body, **{"Idempotency-Key": "k1"})
    r2 = _post(c, f"/v1/preferences/add?turn={turn.pk}", body, **{"Idempotency-Key": "k1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["entry"]["id"] == r2.json()["entry"]["id"]
    assert len(_events(w["person"], "preference.created")) == 1
    r3 = _post(c, f"/v1/preferences/add?turn={turn.pk}", {**body, "preference": "Other."},
               **{"Idempotency-Key": "k1"})
    assert r3.status_code == 409 and r3.json()["type"] == hcp.PROBLEM_BASE + "idempotency-conflict"


# --- the person's own: audit, export, discovery ---------------------------------------


def test_the_audit_log_is_the_persons_paginated_with_an_opaque_cursor(w):
    for n in range(5):
        _add(w["person"], w["ws"], f"Fact {n}.")
    turn = _turn(w["ace"], w["lili"], "t")
    assert _as(w["ace"].user).get(f"{BASE}/v1/audit?turn={turn.pk}").status_code == 403
    c = _as(w["lili"])
    page = c.get(f"{BASE}/v1/audit?limit=2").json()
    assert len(page["events"]) == 2 and page["nextCursor"]
    assert not page["nextCursor"].isdigit()
    nxt = c.get(f"{BASE}/v1/audit?limit=2&cursor={page['nextCursor']}").json()
    assert {e["eventId"] for e in nxt["events"]}.isdisjoint({e["eventId"] for e in page["events"]})
    with pytest.raises(ValueError):
        PersonAuditEvent.objects.first().save()                              # append-only


def test_export_carries_soft_deleted_entries_unless_excluded(w):
    keep = _add(w["person"], w["ws"], "Keep.")
    gone = _add(w["person"], w["ws"], "Gone.")
    people.retract(gone, by=w["lili"])
    c = _as(w["lili"])
    ids = {e["id"] for e in c.get(f"{BASE}/v1/export").json()["entries"]}
    assert ids == {f"urn:uuid:{keep.entry_id}", f"urn:uuid:{gone.entry_id}"}
    ids = {e["id"] for e in c.get(f"{BASE}/v1/export?exclude=deleted&include=audit").json()["entries"]}
    assert ids == {f"urn:uuid:{keep.entry_id}"}
    assert len(_events(w["person"], "preference.exported")) == 2
    turn = _turn(w["ace"], w["lili"], "t")
    assert _as(w["ace"].user).get(f"{BASE}/v1/export?turn={turn.pk}").status_code == 403


def test_discovery_document_is_public_and_declares_the_profile(w, settings):
    # Production's login gate ON: the test settings turn it off, which is how
    # #1343 shipped a "public" document that answered 401 live.
    settings.REQUIRE_AUTH = True
    r = Client().get(f"{BASE}/.well-known/hcp-configuration")
    assert r.status_code == 200
    doc = r.json()
    # ...and only that one path under /api/hcp/ is open.
    assert Client().get(f"{BASE}/v1/grants").status_code == 401
    assert (doc["authorization_profile"], doc["envelope_form"], doc["minimization_method"]) == (
        "first-party", "grouped", "lexical")
    assert doc["supported_categories"] == list(PersonFact.CATEGORIES)


def test_a_wrong_protocol_version_is_refused(w):
    r = _as(w["lili"]).get(f"{BASE}/v1/grants", HTTP_HCP_VERSION="2.0")
    assert r.status_code == 400 and r.json()["type"] == hcp.PROBLEM_BASE + "unsupported-version"


def test_the_privacy_categories_are_refused_at_the_database(w):
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        PersonFact.objects.create(person=w["person"], workspace=w["ws"], kind="role",
                                  statement="x", category="health_context")


# --- the MCP binding (3.2) ------------------------------------------------------


def test_the_four_operations_are_mcp_tools_with_the_spec_names(db):
    from asgiref.sync import async_to_sync

    from apps.mcp.server import mcp

    tools = {t.name: t for t in async_to_sync(mcp.list_tools)()}
    for name in ("hcp_searchPreferences", "hcp_addPreference", "hcp_updatePreference",
                 "hcp_deletePreference"):
        assert name in tools, name
    props = tools["hcp_searchPreferences"].parameters["properties"]
    assert {"query", "categories", "purpose", "maxEntries", "responseDetail", "turn"} <= set(props)
    add = tools["hcp_addPreference"].parameters["properties"]
    assert {"category", "dimension", "preference", "declarationType", "confidence",
            "sourceContext"} <= set(add)
    # Appendix B: grants, audit and export are not MCP tools.
    for name in ("hcp_listAudit", "hcp_listGrants", "hcp_revokeGrant", "hcp_export"):
        assert name not in tools


# --- zero data retention: nothing from a ZDR session is ever written ---------------
#
# Owner rule (2026-10-08). A turn is ZDR when its conversation requires it, when a
# runner flagged `zdr` claimed it, or when it descends from such a turn.


def _zdr_runner():
    from apps.harness.models import Runner, RunnerFlag

    r = Runner.objects.create(name="zdr-box", kind=Runner.CLOUD, capabilities={})
    RunnerFlag.objects.create(runner=r, flag="zdr")
    return r


def _claimed_by(turn, runner):
    Turn.objects.filter(pk=turn.pk).update(claimed_by=runner, status=Turn.RUNNING)
    turn.refresh_from_db()
    return turn


def _add_body(turn):
    return {"category": "work_context", "preference": "Works on KC.",
            "declarationType": "user-declared", "sourceContext": f"turn:{turn.pk}"}


def test_an_hcp_write_from_a_zdr_runners_session_is_refused_and_leaves_nothing(w):
    turn = _claimed_by(_turn(w["ace"], w["lili"], "z"), _zdr_runner())
    r = _post(_as(w["ace"].user), f"/v1/preferences/add?turn={turn.pk}", _add_body(turn))
    assert r.status_code == 403 and "zero data retention" in r.json()["detail"]
    assert not PersonFact.objects.exists()
    assert not _events(w["person"], "preference.created")
    # reads are not writes: the same session may still recall
    assert _search(_as(w["ace"].user), turn).status_code == 200


def test_an_update_from_a_zdr_session_is_refused_but_the_entry_stays(w):
    f = _add(w["person"], w["ws"], "KC metrics lead.")
    turn = _claimed_by(_turn(w["ace"], w["lili"], "z"), _zdr_runner())
    r = _as(w["ace"].user).put(f"{BASE}/v1/preferences/urn:uuid:{f.entry_id}?turn={turn.pk}",
                               data=json.dumps({"updatedPreference": "Changed.", "reason": "r"}),
                               content_type="application/json")
    assert r.status_code == 403
    f.refresh_from_db()
    assert f.is_live and f.statement == "KC metrics lead."


def test_a_conversation_that_requires_zdr_is_zdr_whatever_runner_it_is_on(w):
    from apps.canopy_sessions.models import Session

    chat = Session.objects.create(agent=w["ace"], workspace=w["ws"], title="c",
                                  metadata={"runner_requirements": ["zdr"]})
    turn = _turn(w["ace"], w["lili"], "zc")
    Turn.objects.filter(pk=turn.pk).update(chat_session=chat, agent=None)
    turn.refresh_from_db()
    assert hcp.is_zdr_turn(turn)
    with pytest.raises(hcp.ZdrRefused):
        people.record_fact(person=w["person"], workspace=w["ws"], kind="role", statement="x",
                           by_agent=w["ace"], source_turn=turn)
    assert not PersonFact.objects.exists()


def test_a_turn_descended_from_a_zdr_session_is_zdr_too(w):
    parent = _claimed_by(_turn(w["ace"], w["lili"], "p"), _zdr_runner())
    child = _turn(w["hal"], w["lili"], "c")
    Turn.objects.filter(pk=child.pk).update(parent_turn=parent)
    child.refresh_from_db()
    assert hcp.is_zdr_turn(child)
    assert not hcp.is_zdr_turn(_turn(w["hal"], w["lili"], "plain"))


def test_the_cli_path_is_caught_by_the_parent_turn_header_and_an_unnamed_agent_write_is_refused(w):
    turn = _claimed_by(_turn(w["ace"], w["lili"], "z"), _zdr_runner())
    c = _as(w["ace"].user)
    url = f"/api/people/{w['person'].pk}/facts/"
    body = {"workspace": "connect", "kind": "role", "statement": "KC lead.", "basis": "declared"}
    # `canopy people remember` on a ZDR box: the CLI sends X-Canopy-Parent-Turn
    r = c.post(url, data=json.dumps(body), content_type="application/json",
               HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert r.status_code == 403
    # no turn named at all: an agent's write cannot prove it is not from a ZDR session
    r = c.post(url, data=json.dumps(body), content_type="application/json")
    assert r.status_code == 403 and "must name the turn" in r.json()["detail"]
    assert not PersonFact.objects.exists()
    # a person writing about themself from the web app is not a session: allowed
    plain = _turn(w["ace"], w["lili"], "ok")
    r = c.post(url, data=json.dumps(body), content_type="application/json",
               HTTP_X_CANOPY_PARENT_TURN=str(plain.pk))
    assert r.status_code == 201


def test_zdr_conversations_are_never_handed_to_the_digest(w, settings):
    from apps.harness import people_digest

    settings.PEOPLE_DIGEST_ENABLED = True
    zdr = _claimed_by(_turn(w["ace"], w["lili"], "z", prompt="secret"), _zdr_runner())
    plain = _turn(w["ace"], w["lili"], "p", prompt="hello")
    got = {t.pk for t in people.conversations(w["person"], w["ace"])}
    assert plain.pk in got and zdr.pk not in got
    Turn.objects.filter(pk=zdr.pk).update(status=Turn.DONE)
    zdr.refresh_from_db()
    assert people_digest.on_turn_finished(zdr) is None
