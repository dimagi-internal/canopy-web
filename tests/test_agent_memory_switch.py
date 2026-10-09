"""Agent memory is the PERSON's switch (`Person.hcp_enabled`, Jonathan 2026-10-09:
"each person should be able to turn it on or off").

Off — the default — means no agent reads or writes the person: every HCP
operation is `scope-denied` before a grant is presumed, the legacy fact write
refuses an agent, and the envelope's `person` block carries nothing and says
"off". The person keeps their own entries, export and audit log either way, and
only they can flip it; every flip is on their audit log.
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


def _set(user, enabled):
    return _json(_as(user), "put", "/api/people/me/agent-memory/", {"enabled": enabled})


def _search(c, turn):
    return _json(c, "post", f"/api/hcp/v1/preferences/search?turn={turn.pk}",
                 {"query": "kangaroo care coach", "categories": ["work_context"],
                  "purpose": "answer the question"})


def _add(c, turn):
    return _json(c, "post", f"/api/hcp/v1/preferences/add?turn={turn.pk}",
                 {"category": "work_context", "preference": "Works on KC.",
                  "declarationType": "user-declared", "sourceContext": f"turn:{turn.pk}"})


def test_off_is_the_default_and_every_agent_operation_is_scope_denied(w):
    person, c = w["person"], _as(w["ace"].user)
    assert person.hcp_enabled is False
    people.record_fact(person=person, workspace=w["ws"], kind="role", statement="KC metrics lead.",
                       basis="declared")
    turn = _turn(w["ace"], w["lili"], "t1")
    for r in (_search(c, turn), _add(c, turn)):
        assert r.status_code == 403
        assert r.json()["type"].endswith("/scope-denied")
        assert hcp.MEMORY_OFF in r.json()["detail"]
    entry = person.facts.first()
    r = c.get(f"/api/hcp/v1/preferences/{entry.entry_id}?turn={turn.pk}&purpose=x")
    assert r.status_code == 403
    assert not PersonGrant.objects.filter(person=person).exists()     # nothing presumed


def test_the_legacy_fact_write_refuses_an_agent_for_an_off_person(w):
    r = _json(_as(w["ace"].user), "post", f"/api/people/{w['person'].pk}/facts/",
              {"workspace": "connect", "kind": "role", "statement": "Works on KC.",
               "basis": "declared"})
    assert r.status_code == 403
    assert not w["person"].facts.exists()


def test_the_envelope_says_off_and_serves_nothing(w):
    people.record_fact(person=w["person"], workspace=w["ws"], kind="role",
                       statement="KC metrics lead.", basis="declared")
    env = caller_context.build(_turn(w["ace"], w["lili"], "t1"))
    block = env["person"]
    assert block["hcp"] == "off"
    assert block["facts"] == [] and block["grant"] is None and block["recall"] is None
    assert not PersonGrant.objects.filter(person=w["person"]).exists()
    assert not PersonAccess.objects.filter(person=w["person"]).exists()   # nothing was read


def test_the_person_turns_it_on_and_off_and_each_flip_is_audited(w):
    lili, person, c = w["lili"], w["person"], _as(w["ace"].user)
    r = _set(lili, True)
    assert r.status_code == 200 and r.json()["agent_memory"] is True
    turn = _turn(w["ace"], lili, "t1")
    assert caller_context.build(turn)["person"]["hcp"] == "on"
    assert _add(c, turn).status_code == 201
    assert _search(c, turn).status_code == 200

    assert _set(lili, False).json()["agent_memory"] is False
    assert _search(c, turn).status_code == 403
    # Nothing was deleted, and it is all still the person's.
    me = _as(lili).get("/api/people/me/").json()
    assert me["agent_memory"] is False and len(me["facts"]) == 1
    assert _as(lili).get("/api/hcp/v1/export").status_code == 200
    kinds = list(PersonAuditEvent.objects.filter(person=person, event_type__startswith="agentAccess.")
                 .order_by("pk").values_list("event_type", flat=True))
    assert kinds == ["agentAccess.enabled", "agentAccess.disabled"]
    assert _set(lili, False).status_code == 200                          # a no-op writes nothing
    assert PersonAuditEvent.objects.filter(event_type__startswith="agentAccess.").count() == 2


def test_only_the_person_can_flip_it(w):
    assert _set(w["ace"].user, True).status_code == 403                  # an agent's login
    w["person"].refresh_from_db()
    assert w["person"].hcp_enabled is False


def test_coverage_counts_only_people_who_turned_it_on(w):
    for i in range(coverage.MIN_TURNS_FOR_FACTS):
        _turn(w["ace"], w["lili"], f"t{i}")
    row = coverage.workspace_coverage("connect")["agents"][0]
    assert row["human_turns"] == 0 and row["healthy"] is True          # off: not a gap
    _set(w["lili"], True)
    row = coverage.workspace_coverage("connect")["agents"][0]
    assert row["human_turns"] == coverage.MIN_TURNS_FOR_FACTS and row["healthy"] is False


def test_the_migration_turns_it_on_for_jonathan_only(w):
    jon = User.objects.create_user("jjackson", "jjackson@dimagi.com", "pw")
    EmailAddress.objects.create(user=jon, email="jjackson@dimagi.com", verified=True, primary=True)
    jon_person = contacts.person_for(user=jon)
    mig = importlib.import_module("apps.contacts.migrations.0017_agent_memory_on_for_jonathan")
    mig.forwards(django_apps, None)
    mig.forwards(django_apps, None)                                        # idempotent
    jon_person.refresh_from_db()
    w["person"].refresh_from_db()
    assert jon_person.hcp_enabled is True and w["person"].hcp_enabled is False
    assert PersonAuditEvent.objects.filter(person=jon_person,
                                           event_type="agentAccess.enabled").count() == 1
