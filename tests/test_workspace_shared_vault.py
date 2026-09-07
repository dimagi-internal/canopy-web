"""The tenant's shared vault is set through canopy-web, and never read back.

Companion to the resolve-side tests in test_agent_vault.py. Those assert the
pair REACHES a runner; these assert how it gets there and who may put it there.

The key this route stores must be scoped to the shared vault and nothing else.
A key that also read the per-agent vaults would undo the reason those are split
(Agent.op_vault, 2026-09-06). Nothing in code can enforce that — 1Password
grants it — so the tests pin the parts that ARE enforceable: owner-only, write-
only, encrypted at rest, and non-clobbering.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.common.encryption import encrypt_secret
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/workspaces/acme/shared-vault"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def tenant(client):
    owner = _user("owner")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    client.force_login(owner)
    return {"client": client, "ws": ws, "owner": owner}


def test_an_owner_sets_the_vault_and_its_key(tenant):
    res = tenant["client"].put(
        URL, data={"vault": "Acme-Shared", "service_key": "shared_tok"},
        content_type="application/json",
    )
    assert res.status_code == 200
    assert res.json() == {"vault": "Acme-Shared", "key_set": True}
    tenant["ws"].refresh_from_db()
    assert tenant["ws"].shared_op_vault == "Acme-Shared"


def test_the_key_is_encrypted_and_never_returned(tenant):
    res = tenant["client"].put(
        URL, data={"vault": "Acme-Shared", "service_key": "shared_tok"},
        content_type="application/json",
    )
    assert "shared_tok" not in res.content.decode()
    tenant["ws"].refresh_from_db()
    assert tenant["ws"].shared_op_sa_token_enc
    assert "shared_tok" not in tenant["ws"].shared_op_sa_token_enc
    # And the GET is masked too — key_set is a boolean, not the key.
    got = tenant["client"].get(URL)
    assert got.json() == {"vault": "Acme-Shared", "key_set": True}
    assert "shared_tok" not in got.content.decode()


def test_renaming_the_vault_does_not_wipe_the_key(tenant):
    """Non-clobbering, like every other credential write here. Renaming a vault
    and silently losing the credential that reads it is the shape of bug this
    guards — it would present as the 2026-09-07 outage all over again."""
    tenant["client"].put(URL, data={"vault": "Acme-Shared", "service_key": "shared_tok"},
                         content_type="application/json")
    res = tenant["client"].put(URL, data={"vault": "Acme-Shared-2"},
                               content_type="application/json")
    assert res.json() == {"vault": "Acme-Shared-2", "key_set": True}


def test_an_editor_may_not_set_it(tenant):
    """An editor acts WITHIN a tenant; its credential configuration is an
    owner's call, matching invites and member roles."""
    ed = _user("ed")
    WorkspaceMembership.objects.create(
        workspace=tenant["ws"], user=ed, role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(ed)
    assert c.put(URL, data={"vault": "x"}, content_type="application/json").status_code == 403
    assert c.get(URL).status_code == 403


def test_a_non_member_gets_404_not_403(tenant):
    """No existence leak, matching the rest of the workspace surface: a
    non-member must not be able to tell a tenant apart from a typo."""
    c = Client()
    c.force_login(_user("outsider"))
    assert c.get(URL).status_code == 404
    assert c.put(URL, data={"vault": "x"}, content_type="application/json").status_code == 404


def test_an_unset_tenant_reports_empty_not_an_error(tenant):
    assert tenant["client"].get(URL).json() == {"vault": "", "key_set": False}


def test_a_blank_key_leaves_an_existing_one_alone(tenant):
    """The empty string is not a way to clear the key by accident — same
    non-clobbering rule, stated for the value a form posts when left untouched."""
    tenant["ws"].shared_op_sa_token_enc = encrypt_secret("shared_tok")
    tenant["ws"].save(update_fields=["shared_op_sa_token_enc"])
    res = tenant["client"].put(URL, data={"vault": "Acme-Shared", "service_key": ""},
                               content_type="application/json")
    assert res.json()["key_set"] is True
