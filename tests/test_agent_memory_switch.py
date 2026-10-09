"""Agent memory is the PERSON's own, as two independent features (Jonathan,
2026-10-09), each with a canopy-level `available` + `default` and a per-session
override:

* record — "Agents may learn about me": agents may WRITE;
* use — "Agents may use what they've learned": agents may READ.

Effective = available AND (session override, else default). Everything starts
unavailable; any combination is valid. A refused operation is `scope-denied`
before a grant is presumed; the legacy fact write refuses an agent without
`record`; the envelope's `person` block carries `hcp: {record, use}`, facts and
`recall` only with `use`, and `record` only with `record`. The person keeps their
own entries, export and audit log whatever the switches say, only they can flip
them, and each flip is its own audit event.
"""
from __future__ import annotations

import importlib
import json

import pytest
from allauth.account.models import EmailAddress
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import coverage, hcp, people
from apps.contacts import services as contacts
from apps.contacts.models import PersonAccess, PersonAuditEvent, PersonGrant
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _member(ws, username):
    u = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return u


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = _member(ws, "lili")
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=owner)
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])
    return {"ws": ws, "lili": lili, "ace": agent, "owner": owner,
            "person": contacts.person_for(user=lili)}


def _turn(agent, user, key, prompt="kangaroo care coach"):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=prompt,
        initiator=who.for_user(user, via="chat", assurance=who.SESSION))
    return turn


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _json(c, method, path, body):
    return getattr(c, method)(path, data=json.dumps(body), content_type="application/json")


def _set(user, **switches):
    """`record=True` → available + on by default; `record=False` → unavailable."""
    body = {f: ({"available": True, "default": True} if v else {"available": False})
            for f, v in switches.items()}
    return _json(_as(user), "put", "/api/people/me/agent-memory/", body)


def _session(user, agent):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(agent=agent, created_by=user, workspace=agent.workspace)


def _session_turn(agent, user, session, key, prompt="kangaroo care coach"):
    turn, _ = services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=prompt,
        initiator=who.for_user(user, via="chat", assurance=who.SESSION))
    assert turn.chat_session_id == session.pk
    return turn


def _set_session(user, session, **choices):
    return _json(_as(user), "put", f"/api/people/me/sessions/{session.pk}/agent-memory/", choices)


def _eff(block):
    return {"record": block["hcp"]["record"], "use": block["hcp"]["use"]}


def _search(c, turn):
    return _json(c, "post", f"/api/hcp/v1/preferences/search?turn={turn.pk}",
                 {"query": "kangaroo care coach", "categories": ["work_context"],
                  "purpose": "answer the question"})


def _add(c, turn, statement="Works on KC."):
    return _json(c, "post", f"/api/hcp/v1/preferences/add?turn={turn.pk}",
                 {"category": "work_context", "preference": statement,
                  "declarationType": "user-declared", "sourceContext": f"turn:{turn.pk}"})


def _seed(w, statement="KC metrics lead."):
    return people.record_fact(person=w["person"], workspace=w["ws"], kind="role",
                              statement=statement, basis="declared")


def _denied(r, detail):
    assert r.status_code == 403
    assert r.json()["type"].endswith("/scope-denied")
    assert detail in r.json()["detail"]


def test_everything_unavailable_by_default_and_every_agent_operation_is_scope_denied(w):
    person, c = w["person"], _as(w["ace"].user)
    assert (person.hcp_record_available, person.hcp_use_available) == (False, False)
    entry = _seed(w)
    turn = _turn(w["ace"], w["lili"], "t1")
    _denied(_search(c, turn), hcp.USE_OFF)
    _denied(_add(c, turn), hcp.RECORD_OFF)
    _denied(c.get(f"/api/hcp/v1/preferences/{entry.entry_id}?turn={turn.pk}&purpose=x"),
            hcp.USE_OFF)
    assert not PersonGrant.objects.filter(person=person).exists()     # nothing presumed
    block = caller_context.build(turn)["person"]
    assert _eff(block) == {"record": False, "use": False}
    assert block["hcp"]["available"] == {"record": False, "use": False}
    assert block["facts"] == [] and block["grant"] is None
    assert block["recall"] is None and block["record"] is None
    assert not PersonAccess.objects.filter(person=person).exists()    # nothing was read


def test_record_only_writes_but_never_reads(w):
    _set(w["lili"], record=True)
    _seed(w)
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t1")
    assert _add(c, turn).status_code == 201
    _denied(_search(c, turn), hcp.USE_OFF)
    block = caller_context.build(turn)["person"]
    assert _eff(block) == {"record": True, "use": False}
    assert block["facts"] == [] and block["recall"] is None
    assert block["record"]["tool"] == "hcp_addPreference" and block["record"]["turn"] == str(turn.pk)
    assert not PersonAccess.objects.filter(person=w["person"]).exists()


def test_use_only_reads_but_never_writes(w):
    _set(w["lili"], use=True)
    _seed(w)
    c, turn = _as(w["ace"].user), _turn(w["ace"], w["lili"], "t1")
    assert _search(c, turn).status_code == 200
    _denied(_add(c, turn), hcp.RECORD_OFF)
    block = caller_context.build(turn)["person"]
    assert _eff(block) == {"record": False, "use": True}
    assert [f["statement"] for f in block["facts"]] == ["KC metrics lead."]
    assert block["recall"] is not None and block["record"] is None


def test_available_but_off_by_default_is_off_until_a_session_turns_it_on(w):
    lili, ace = w["lili"], w["ace"]
    _json(_as(lili), "put", "/api/people/me/agent-memory/",
          {"record": {"available": True, "default": True}, "use": {"available": True, "default": False}})
    _seed(w)
    c = _as(ace.user)
    plain = _turn(ace, lili, "t0")                                    # no session: defaults
    _denied(_search(c, plain), hcp.USE_OFF)
    session = _session(lili, ace)
    t1 = _session_turn(ace, lili, session, "t1")
    _denied(_search(c, t1), hcp.USE_OFF)
    r = _set_session(lili, session, use="on")
    assert r.status_code == 200 and r.json()["use"] == {
        "available": True, "default": False, "override": True, "effective": True}
    assert _search(c, t1).status_code == 200
    assert _eff(caller_context.build(t1)["person"]) == {"record": True, "use": True}
    _denied(_search(c, plain), hcp.USE_OFF)                           # other turns unchanged
    _set_session(lili, session, record="off")
    _denied(_add(c, t1), hcp.RECORD_OFF)
    assert _add(c, plain).status_code == 201
    _set_session(lili, session, record="inherit", use="inherit")
    assert _eff(caller_context.build(t1)["person"]) == {"record": True, "use": False}


def test_a_session_cannot_turn_on_what_is_not_available(w):
    lili, ace = w["lili"], w["ace"]
    _set(lili, record=True)                                           # use unavailable
    session = _session(lili, ace)
    r = _set_session(lili, session, use="on")
    assert r.status_code == 403
    assert _as(lili).get(f"/api/people/me/sessions/{session.pk}/agent-memory/").json()["use"] == {
        "available": False, "default": False, "override": None, "effective": False}
    _set_session(lili, session, record="on")
    _set(lili, record=False)                                          # made unavailable later
    t = _session_turn(ace, lili, session, "t1")
    _denied(_add(_as(ace.user), t), hcp.RECORD_OFF)                   # the override cannot widen it


def test_only_the_sessions_own_person_can_set_it(w):
    lili, ace, owner = w["lili"], w["ace"], w["owner"]
    _set(lili, record=True)
    session = _session(lili, ace)
    assert _set_session(ace.user, session, record="off").status_code == 403   # an agent's login
    assert _set_session(owner, session, record="off").status_code == 404      # someone else
    assert _set_session(lili, session, record="maybe").status_code == 422
    assert _set_session(lili, session, record="off").status_code == 200


def test_session_changes_are_audited(w):
    lili = w["lili"]
    _set(lili, record=True)
    session = _session(lili, w["ace"])
    _set_session(lili, session, record="off")
    _set_session(lili, session, record="off")                         # a no-op writes nothing
    _set_session(lili, session, record="inherit")
    kinds = list(PersonAuditEvent.objects.filter(person=w["person"], event_type__startswith="session")
                 .order_by("pk").values_list("event_type", flat=True))
    assert kinds == ["sessionRecord.off", "sessionRecord.inherit"]


def test_the_legacy_fact_write_needs_record(w):
    turn = _turn(w["ace"], w["lili"], "t1")

    def write():
        return _json(_as(w["ace"].user), "post", f"/api/people/{w['person'].pk}/facts/",
                     {"workspace": "connect", "kind": "role", "statement": "Works on KC.",
                      "basis": "declared", "source_turn_id": str(turn.pk)})
    _set(w["lili"], use=True)
    assert write().status_code == 403
    _set(w["lili"], record=True)
    assert write().status_code == 201


def test_each_change_is_its_own_audit_event_and_off_deletes_nothing(w):
    lili, person = w["lili"], w["person"]
    r = _set(lili, record=True, use=True)
    assert r.status_code == 200
    assert {f: (r.json()[f]["available"], r.json()[f]["default"]) for f in ("record", "use")} == {
        "record": (True, True), "use": (True, True)}
    assert _add(_as(w["ace"].user), _turn(w["ace"], lili, "t1")).status_code == 201
    _json(_as(lili), "put", "/api/people/me/agent-memory/", {"use": {"default": False}})
    _set(lili, record=False)
    me = _as(lili).get("/api/people/me/").json()
    assert me["agent_memory"]["record"]["available"] is False
    assert (me["agent_memory"]["use"]["available"], me["agent_memory"]["use"]["default"]) == (True, False)
    assert len(me["facts"]) == 1                                         # nothing deleted
    assert _as(lili).get("/api/hcp/v1/export").status_code == 200
    kinds = list(PersonAuditEvent.objects.filter(person=person, event_type__startswith="agent")
                 .order_by("pk").values_list("event_type", flat=True))
    assert kinds == ["agentRecord.enabled", "agentRecord.defaultOn", "agentUse.enabled",
                     "agentUse.defaultOn", "agentUse.defaultOff", "agentRecord.disabled"]
    _set(lili, record=False)                                              # a no-op writes nothing
    assert PersonAuditEvent.objects.filter(event_type__startswith="agent").count() == 6


def test_only_the_person_can_change_them(w):
    assert _set(w["ace"].user, record=True, use=True).status_code == 403   # an agent's login
    w["person"].refresh_from_db()
    assert (w["person"].hcp_record_available, w["person"].hcp_use_available) == (False, False)


def test_coverage_counts_only_people_who_make_recording_available(w):
    for i in range(coverage.MIN_TURNS_FOR_FACTS):
        _turn(w["ace"], w["lili"], f"t{i}")
    _set(w["lili"], use=True)
    row = coverage.workspace_coverage("connect")["agents"][0]
    assert row["human_turns"] == 0 and row["healthy"] is True          # nothing may be recorded
    _set(w["lili"], record=True)
    row = coverage.workspace_coverage("connect")["agents"][0]
    assert row["human_turns"] == coverage.MIN_TURNS_FOR_FACTS and row["healthy"] is False


def test_the_migration_sets_record_yes_use_no_for_jonathan_only(w):
    jon = User.objects.create_user("jjackson", "jjackson@dimagi.com", "pw")
    EmailAddress.objects.create(user=jon, email="jjackson@dimagi.com", verified=True, primary=True)
    jon_person = contacts.person_for(user=jon)
    jon_person.hcp_record_available = jon_person.hcp_use_available = True   # as 0017 left it
    jon_person.save()
    mig = importlib.import_module("apps.contacts.migrations.0019_agent_memory_defaults")
    mig.forwards(django_apps, None)
    mig.forwards(django_apps, None)                                        # idempotent
    jon_person.refresh_from_db()
    w["person"].refresh_from_db()
    assert (jon_person.hcp_record_available, jon_person.hcp_record_default) == (True, True)
    assert (jon_person.hcp_use_available, jon_person.hcp_use_default) == (False, False)
    assert (w["person"].hcp_record_available, w["person"].hcp_use_available) == (False, False)
    assert sorted(PersonAuditEvent.objects.filter(person=jon_person)
                  .values_list("event_type", flat=True)) == ["agentRecord.defaultOn",
                                                             "agentUse.disabled"]
