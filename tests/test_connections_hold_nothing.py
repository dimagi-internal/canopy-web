"""canopy-web holds no OAuth token it could use: no token column on `Connection`,
no Fernet anywhere in the connections app but the app's client secret, and no
route that hands a token out. A connection row carries the sealed envelope only."""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.connections import wire
from apps.connections.models import Connection, ConnectionGrant
from apps.workspaces.models import Workspace
from tests.fixtures.connections import _stored, home

pytestmark = pytest.mark.django_db

TOKEN_COLUMNS = {"refresh_token_enc", "access_token_enc", "access_token_expires_at", "access_token_seconds"}
APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps" / "connections"


def _inside(tree: ast.AST, node: ast.AST, class_name: str, func_name: str) -> bool:
    """Whether `node` sits in a function `func_name` of class `class_name` in `tree`."""
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for fn in ast.walk(cls):
            if (isinstance(fn, ast.FunctionDef) and fn.name == func_name
                    and any(n is node for n in ast.walk(fn))):
                return True
    return False


def test_fernet_is_used_only_for_the_app_client_secret():
    paths = list(APP_DIR.rglob("*.py"))
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", "")) in {"encrypt_secret", "decrypt_secret"}]
        if path.name == "models.py":
            assert calls and all(_inside(tree, c, "ConnectionApp", "client_secret") for c in calls), path
        else:
            assert calls == [], path
        # Nor any direct use of Fernet, imported or called.
        fernet = [n for n in ast.walk(tree)
                  if (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("cryptography.fernet"))
                  or (isinstance(n, ast.Import) and any(a.name.startswith("cryptography.fernet") for a in n.names))
                  or (isinstance(n, ast.Name) and n.id == "Fernet")
                  or (isinstance(n, ast.Attribute) and n.attr == "Fernet")]
        assert fernet == [], f"{path} uses Fernet"


def test_no_connection_column_holds_a_token():
    assert not {f.name for f in Connection._meta.get_fields()} & TOKEN_COLUMNS


def test_old_token_routes_are_gone(client):
    assert client.post("/api/me/connections/salesforce/token").status_code == 404
    rid, tid = uuid.uuid4(), uuid.uuid4()
    assert client.post(f"/api/harness/runners/{rid}/turns/{tid}/connection-token").status_code == 404


def test_the_grant_routes_are_gone(client):
    cid = uuid.uuid4()
    assert client.put(f"/api/agents/eva/connections/{cid}/grants/echo").status_code == 404
    assert client.delete(f"/api/agents/eva/connections/{cid}/grants/echo").status_code == 404


def test_a_person_connection_may_be_granted():
    user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=home(user))
    person_conn = _stored({"user": user}, user)
    ConnectionGrant.objects.create(connection=person_conn, agent=agent)  # no ValidationError
    assert ConnectionGrant.objects.filter(connection=person_conn, agent=agent).exists()


def test_a_connection_row_is_sealed_and_bound_to_its_uuid():
    user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    conn = _stored({"user": user}, user)
    assert isinstance(conn.pk, uuid.UUID)
    assert conn.sealed.startswith("zks1:")
    assert conn.state == Connection.UNSHARED
    assert conn.binding == wire.binding_id(conn.id) == f"connection:{conn.id}"
    assert conn.statement_version == 0 and conn.access_sealed == ""
    assert conn.needs_reconnect is False


def test_an_app_can_be_marked_removed():
    user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    from tests.fixtures.connections import sf_app

    app = sf_app(ws)
    assert app.removed_at is None
