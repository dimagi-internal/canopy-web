"""A person with NO workspace keeps their own HCP instance (docs/architecture/hcp-service.md).

Jonathan, 2026-10-09: HCP "could be exposed for anyone even if they don't yet
belong to a workspace". So a signed-in human with no WorkspaceMembership can
write PERSONAL entries (workspace NULL), find, correct and export them, and
manage their grants and audit log — and no workspace's agent is ever told a
personal entry.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import services as contacts
from apps.contacts.models import PersonAuditEvent, PersonFact
from apps.harness import services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "/api/hcp"


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(c, path, body):
    return c.post(BASE + path, data=json.dumps(body), content_type="application/json")


@pytest.fixture()
def loner():
    """Signed in, a member of nothing."""
    return User.objects.create_user("solo", "solo@dimagi.com", "pw")


def _add(c, text, category="general_preferences"):
    return _post(c, "/v1/preferences/add", {"category": category, "preference": text,
                                            "declarationType": "user-declared",
                                            "sourceContext": "user-input"})


def test_a_person_with_no_workspace_writes_a_personal_entry(loner):
    assert not WorkspaceMembership.objects.filter(user=loner).exists()
    r = _add(_as(loner), "I prefer one short answer with links.")
    assert r.status_code == 201, r.content
    entry = r.json()["entry"]
    assert entry["record"]["metadata"]["canopy:workspace"] is None
    assert entry["claim"]["issuer"]["id"].endswith("/people/me")
    fact = PersonFact.objects.get()
    assert fact.workspace_id is None and fact.person == contacts.person_for(user=loner)
    assert PersonAuditEvent.objects.filter(event_type="preference.created",
                                           workspace__isnull=True).count() == 1


def test_they_can_find_correct_and_export_it(loner):
    c = _as(loner)
    entry_id = _add(c, "I prefer one short answer with links.").json()["entry"]["id"]

    r = _post(c, "/v1/preferences/search", {"query": "short answer links",
                                            "categories": ["general_preferences"],
                                            "purpose": "checking what I saved"})
    assert r.status_code == 200, r.content
    assert [e["id"] for e in r.json()["entries"]] == [entry_id]

    r = c.put(f"{BASE}/v1/preferences/{entry_id}",
              data=json.dumps({"updatedPreference": "I prefer a short answer, links last.",
                               "reason": "fix"}), content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["entry"]["record"]["version"] == 2

    r = c.get(f"{BASE}/v1/export?include=audit,versions")
    assert r.status_code == 200
    body = r.json()
    assert [e["claim"]["subject"]["preference"] for e in body["entries"]] == [
        "I prefer a short answer, links last."]
    assert len(body["versions"]) == 1 and body["audit"]

    assert c.get(f"{BASE}/v1/grants?status=all").status_code == 200
    assert c.get(f"{BASE}/v1/audit").status_code == 200


def test_people_me_shows_a_personal_entry_with_no_workspace(loner):
    c = _as(loner)
    _add(c, "Mornings are best for me.", category="coordination_context")
    r = c.get("/api/people/me/")
    assert r.status_code == 200, r.content
    [fact] = r.json()["facts"]
    assert fact["workspace"] is None and fact["statement"] == "Mornings are best for me."


def test_naming_a_workspace_they_are_not_in_is_refused(loner):
    owner = User.objects.create_user("own", "own@dimagi.com", "pw")
    Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    r = _post(_as(loner), "/v1/preferences/add?workspace=other",
              {"category": "work_context", "preference": "x y z", "declarationType": "user-declared",
               "sourceContext": "user-input"})
    assert r.status_code == 403


def test_no_workspace_agent_is_told_a_personal_entry(agents_granted):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=owner)
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])

    _add(_as(lili), "Kangaroo care coach questions mean the KC audit coach.", category="work_context")
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1", prompt="kangaroo care coach",
        initiator=who.for_user(lili, via="chat", assurance=who.SESSION))
    r = _post(_as(login), f"/v1/preferences/search?turn={turn.pk}",
              {"query": "kangaroo care coach", "categories": ["work_context"], "purpose": "answer"})
    assert r.status_code == 200, r.content
    assert r.json()["entries"] == []
