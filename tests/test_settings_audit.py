"""The settings audit — one plumbing for every settings change (`apps/events/audit.py`).

Pins the contract every SETTINGS route gets without writing a line of its own:
a row per change naming who and through what (web UI, CLI, other token, MCP),
the request with secrets withheld, the change and its row committing together,
nothing for a refused write or a write that is not a setting, and a person's own
settings visible to them alone.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.events import audit
from apps.events.models import SettingChange
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    viewer = User.objects.create_user(username="v", email="viewer@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    return {"jj": jj, "viewer": viewer, "ws": ws, "ace": ace}


def _session(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _pat(user, **headers) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="laptop")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}", **headers)


def _patch(c, path, body):
    return c.patch(path, data=json.dumps(body), content_type="application/json")


def _put(c, path, body):
    return c.put(path, data=json.dumps(body), content_type="application/json")


def test_a_setting_changed_in_the_ui_is_recorded(fleet):
    res = _patch(_session(fleet["jj"]), "/api/agents/ace/turn-mode", {"turn_mode": "auto"})
    assert res.status_code == 200, res.content
    [row] = SettingChange.objects.all()
    assert (row.operation, row.via, row.actor_email, row.workspace) == (
        "set_turn_mode", "ui", "jj@dimagi.com", fleet["ws"])
    assert row.target == "ace"
    assert row.request["body"] == {"turn_mode": "auto"}
    assert row.request_id


@pytest.mark.parametrize("headers,via", [
    ({"HTTP_X_CANOPY_CLIENT": "canopy-cli/0.2.665"}, "cli"),
    ({}, "api"),
])
def test_a_token_caller_is_named_with_its_credential(fleet, headers, via):
    res = _patch(_pat(fleet["jj"], **headers), "/api/agents/ace/turn-mode", {"turn_mode": "auto"})
    assert res.status_code == 200, res.content
    [row] = SettingChange.objects.all()
    assert row.via == via
    assert row.credential["label"] == "laptop"
    # The credential is named, never carried: type, id and label only.
    assert set(row.credential) <= {"type", "id", "label"}


def test_an_mcp_call_is_named_as_mcp():
    assert audit._via({"via_mcp": True}, {"type": "pat"}) == "mcp"
    assert audit._via({}, {"type": "session"}) == "ui"
    assert audit._via({"client": "canopy-cli/1"}, {"type": "pat"}) == "cli"


def test_secrets_in_the_body_are_withheld(fleet):
    res = _put(_session(fleet["jj"]), "/api/agents/ace/vault",
               {"vault": "Agent-Ace", "service_key": "ops_SECRETVALUE"})
    assert res.status_code in (200, 201), res.content
    [row] = SettingChange.objects.filter(operation="set_agent_vault")
    assert "ops_SECRETVALUE" not in json.dumps(row.request)
    assert row.request["body"]["service_key"] == audit.REDACTED


def test_redact_reaches_any_depth():
    got = audit.redact({"a": {"client_secret": "x", "items": [{"api_token": "y", "name": "n"}]}})
    assert got == {"a": {"client_secret": audit.REDACTED,
                         "items": [{"api_token": audit.REDACTED, "name": "n"}]}}


def test_no_change_without_its_record(fleet, monkeypatch):
    """The row is written inside the change's transaction: if it cannot be
    written, the change does not happen."""
    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(SettingChange.objects, "create", boom)
    c = _session(fleet["jj"])
    c.raise_request_exception = False
    res = _patch(c, "/api/agents/ace/turn-mode", {"turn_mode": "auto"})
    assert res.status_code == 500
    fleet["ace"].refresh_from_db()
    assert fleet["ace"].turn_mode != "auto"


def test_a_refused_setting_write_records_nothing(fleet):
    res = _patch(_session(fleet["viewer"]), "/api/agents/ace/turn-mode", {"turn_mode": "auto"})
    assert res.status_code in (403, 404)
    assert not SettingChange.objects.exists()


def test_a_write_that_is_not_a_setting_records_nothing(fleet):
    c = _session(fleet["jj"])
    res = c.post("/api/agents/ace/projects/", data=json.dumps({"name": "P"}),
                 content_type="application/json")
    assert res.status_code in (200, 201), res.content
    assert not SettingChange.objects.exists()


def test_a_persons_own_setting_is_theirs_alone(fleet):
    res = _patch(_session(fleet["viewer"]), "/api/me/presence-preference/", {"show_presence": False})
    assert res.status_code == 200, res.content
    [row] = SettingChange.objects.all()
    assert row.workspace is None and row.actor == fleet["viewer"]
    mine = _session(fleet["viewer"]).get("/api/events/setting-changes").json()["items"]
    assert [i["operation"] for i in mine] == ["set_presence_preference"]
    # The workspace owner reads the workspace's changes — not a member's own settings.
    theirs = _session(fleet["jj"]).get("/api/events/setting-changes").json()["items"]
    assert theirs == []


def test_a_viewer_does_not_read_the_workspaces_changes(fleet):
    _patch(_session(fleet["jj"]), "/api/agents/ace/turn-mode", {"turn_mode": "auto"})
    assert _session(fleet["viewer"]).get("/api/events/setting-changes").json()["items"] == []
    [item] = _session(fleet["jj"]).get("/api/events/setting-changes").json()["items"]
    assert item["operation"] == "set_turn_mode" and item["via"] == "ui"
