"""Shared fixtures: a workspace with an owner, an editor, a viewer and an outsider, and Hiring installed."""
from __future__ import annotations

import pytest

from apps.workspace_apps.models import AppInstallation, AppRoleGrant
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.workspace_apps import _client, _user


@pytest.fixture()
def world():
    admin, editor, viewer, outsider = (_user(n) for n in ("admin", "editor", "viewer", "outsider"))
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=admin)
    Workspace.objects.create(slug="other", display_name="Other", created_by=outsider)
    for u, role in ((admin, WorkspaceMembership.OWNER), (editor, WorkspaceMembership.EDITOR),
                    (viewer, WorkspaceMembership.VIEWER)):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    WorkspaceMembership.objects.create(user=outsider, workspace_id="other", role=WorkspaceMembership.OWNER)
    return dict(ws=ws, admin=admin, editor=editor, viewer=viewer, outsider=outsider)


@pytest.fixture()
def hiring(world):
    r = _client(world["admin"]).post("/api/workspace-apps/dimagi/", {"template": "hiring"}, content_type="application/json")
    assert r.status_code == 201, r.content
    inst = AppInstallation.objects.get(workspace_id="dimagi", key="hiring")
    # Recruiter goes to admins and to people named for it — the editor here is the recruiter.
    AppRoleGrant.objects.create(installation=inst, user=world["editor"], role="recruiter", granted_by=world["admin"])
    return inst


