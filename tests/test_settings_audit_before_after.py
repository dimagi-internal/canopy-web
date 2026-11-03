"""Before and after for every setting change, from the one plumbing (`apps/events/audit.py`).

A `setting=True` route writes no audit code of its own and still gets, in its
`SettingChange.changes`, each row it saved or deleted with the fields that changed:
a create is before=None, a delete after=None, a save that changed nothing is not
there. Secrets are withheld, the audit's own tables are not settings, a handler's
own `note_change` wins, and a refused write records nothing.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from ninja.errors import HttpError

from apps.agents.models import Agent, AgentCredential
from apps.connections.models import ConnectionApp
from apps.api.route_gates import gate
from apps.api import change_capture
from apps.events import audit
from apps.events.models import Event, SettingChange
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    return {"jj": jj, "ws": ws, "ace": ace}


def _run(fleet, view, monkeypatch, method="post"):
    """Call `view` as an audited setting route, as the API would."""
    monkeypatch.setattr(audit, "operation_id", lambda func: func.__name__)
    request = getattr(RequestFactory(), method)("/api/agents/ace/")
    request.user = fleet["jj"]
    return audit.audited(gate("agent.work", setting=True)(view))(request, slug="ace")


def test_a_route_with_no_audit_code_gets_its_before_and_after(fleet):
    c = Client()
    c.force_login(fleet["jj"])
    res = c.patch("/api/agents/ace/turn-mode", data=json.dumps({"turn_mode": "auto"}),
                  content_type="application/json")
    assert res.status_code == 200, res.content
    [row] = SettingChange.objects.all()
    assert row.changes == [{"scope": f"agents.agent:{fleet['ace'].pk}",
                            "before": {"turn_mode": "manual"}, "after": {"turn_mode": "auto"}}]


def test_a_create_and_a_delete(fleet, monkeypatch):
    def create(request, slug):
        return Workspace.objects.create(slug="labs", display_name="Labs", created_by=fleet["jj"])

    _run(fleet, create, monkeypatch)
    labs = Workspace.objects.get(slug="labs")
    # The workspace, and what creating one seeds (its admission rules).
    changes = SettingChange.objects.get().changes
    [created] = [c for c in changes if c["scope"] == f"workspaces.workspace:{labs.pk}"]
    assert all(c["scope"].startswith("admission.admissionrule:") for c in changes if c is not created)
    assert created["scope"] == f"workspaces.workspace:{labs.pk}" and created["before"] is None
    assert created["after"]["display_name"] == "Labs"
    assert "created_at" not in created["after"]  # the database's own timestamp

    SettingChange.objects.all().delete()

    def remove(request, slug):
        Workspace.objects.get(slug="labs").delete()

    _run(fleet, remove, monkeypatch, method="delete")
    [gone] = SettingChange.objects.get().changes
    assert gone["scope"] == f"workspaces.workspace:{labs.pk}" and gone["after"] is None
    assert gone["before"]["display_name"] == "Labs"


def test_a_delete_names_what_cascaded_with_it(fleet, monkeypatch):
    def remove(request, slug):
        Agent.objects.filter(slug="ace").delete()

    AgentCredential.objects.create(agent=fleet["ace"], name="X_TOKEN")
    _run(fleet, remove, monkeypatch, method="delete")
    [gone] = SettingChange.objects.get().changes
    assert gone["before"]["slug"] == "ace"
    assert gone["also_deleted"]["agents.AgentCredential"] == 1


def test_a_queryset_update_and_a_bulk_create_are_seen_too(fleet, monkeypatch):
    """Neither sends a model signal; both are captured all the same."""
    def write(request, slug):
        Agent.objects.filter(slug="ace").update(turn_mode="auto")
        AgentCredential.objects.bulk_create([AgentCredential(agent=fleet["ace"], name="X_TOKEN")])

    _run(fleet, write, monkeypatch)
    updated, created = SettingChange.objects.get().changes
    assert updated == {"scope": f"agents.agent:{fleet['ace'].pk}",
                       "before": {"turn_mode": "manual"}, "after": {"turn_mode": "auto"}}
    assert created["scope"].startswith("agents.agentcredential:") and created["before"] is None
    assert created["after"]["name"] == "X_TOKEN"


def test_a_save_that_changes_nothing_is_not_a_change(fleet, monkeypatch):
    def resave(request, slug):
        agent = Agent.objects.get(slug="ace")
        agent.save()

    _run(fleet, resave, monkeypatch)
    # The row is still there (the body is the record); no before/after to show.
    assert SettingChange.objects.get().changes == []


def test_secrets_are_withheld(fleet, monkeypatch):
    def store(request, slug):
        ConnectionApp.objects.create(workspace=fleet["ws"], provider="salesforce", client_id="cid",
                                     client_secret_enc="ciphertext-of-ghp_SECRET")

    _run(fleet, store, monkeypatch)
    row = SettingChange.objects.get()
    assert "ghp_SECRET" not in json.dumps(row.changes)
    assert row.changes[0]["before"] is None
    assert row.changes[0]["after"]["client_secret_enc"] == audit.REDACTED


def test_logs_are_not_settings(fleet, monkeypatch):
    def log(request, slug):
        Event.objects.create(workspace=fleet["ws"], source="t", kind="t", level="info")

    _run(fleet, log, monkeypatch)
    assert SettingChange.objects.get().changes == []


def test_a_handlers_own_notes_win(fleet, monkeypatch):
    def noted(request, slug):
        agent = Agent.objects.get(slug="ace")
        agent.turn_mode = "auto"
        agent.save()
        audit.note_change(scope="agent:ace/turn-mode", before="manual", after="auto")

    _run(fleet, noted, monkeypatch)
    assert SettingChange.objects.get().changes == [
        {"scope": "agent:ace/turn-mode", "before": "manual", "after": "auto"}]


def test_a_refused_write_records_nothing(fleet, monkeypatch):
    def refused(request, slug):
        agent = Agent.objects.get(slug="ace")
        agent.turn_mode = "auto"
        agent.save()
        raise HttpError(403, "no")

    with pytest.raises(HttpError):
        _run(fleet, refused, monkeypatch)
    assert not SettingChange.objects.exists()
    assert Agent.objects.get(slug="ace").turn_mode == "manual"


def test_nothing_is_captured_outside_a_setting_route(fleet):
    agent = Agent.objects.get(slug="ace")
    agent.turn_mode = "auto"
    agent.save()
    agent.delete()
    assert change_capture._current.get() is None
    assert not SettingChange.objects.exists()


def test_deletes_keep_djangos_fast_path():
    """No delete listener: a cascade through a high-volume table stays one query."""
    from django.db.models.signals import post_delete, pre_delete

    from apps.events.models import Activity

    assert not pre_delete.has_listeners(Activity) and not post_delete.has_listeners(Activity)
