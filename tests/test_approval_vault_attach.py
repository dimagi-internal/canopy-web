"""`vault.attach_agent`: the approval that stands in for attaching an agent on a vault's page.

Pinned: the authority is the route's (a manager of the vault who also administers the agent); approving
needs a fresh passkey and only the web page can supply it; a Slack click or alert button is refused; an
agent outside the vault's workspace tree fails with nothing changed, as the route's 422 does.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.credentials import workspace_vaults
from apps.agents.models import Agent, VaultAttachment
from apps.approvals import kinds, services
from apps.approvals.models import ApprovalRequest
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

J = "application/json"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def w():
    class W:
        pass

    w = W()
    w.jj, w.beth = _user("jj"), _user("beth")
    w.dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w.jj)
    w.connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=w.jj, parent=w.dimagi)
    WorkspaceMembership.objects.create(workspace=w.dimagi, user=w.jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=w.dimagi, user=w.beth, role=WorkspaceMembership.EDITOR)
    w.budgy = Agent.objects.create(slug="budgy", name="Budgy", workspace=w.dimagi, owner=w.beth)
    w.hal = Agent.objects.create(slug="hal", name="Hal", workspace=w.connect, owner=w.jj)
    w.vault = workspace_vaults.create(w.dimagi, "Salesforce (Eva's account)", by=w.jj)
    w.connect_vault = workspace_vaults.create(w.connect, "Connect only", by=w.jj)
    return w


def _file(vault, agent, by):
    req, _ = services.create(kind="vault.attach_agent", params={"vault_id": str(vault.pk), "agent": agent.slug},
                             requested_by_user=by)
    return req


def _decide(user, req, *, step_up, decision="approve"):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c.post(f"/api/approvals/{req.pk}/decide", {"decision": decision}, content_type=J)


def test_the_preview_names_the_vault_and_the_agent_and_says_a_passkey_is_needed(w):
    d = kinds.get("vault.attach_agent").describe(kinds.parse("vault.attach_agent", {
        "vault_id": str(w.vault.pk), "agent": "budgy"})[1])
    assert "Budgy" in d.title and "Salesforce (Eva's account)" in d.title
    assert any("passkey" in line for line in d.plan)


def test_only_a_manager_of_the_vault_who_also_administers_the_agent_may_approve(w):
    k, p = kinds.parse("vault.attach_agent", {"vault_id": str(w.vault.pk), "agent": "budgy"})
    d = k.describe(p)
    assert k.may_approve(w.jj, p, d)          # owns the vault and the dimagi workspace, so administers budgy
    assert not k.may_approve(w.beth, p, d)    # owns budgy, but only a member of the vault


def test_approving_attaches_the_agent_when_the_web_page_has_a_fresh_passkey(w):
    req = _file(w.vault, w.budgy, w.beth)
    r = _decide(w.jj, req, step_up=True)
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert VaultAttachment.objects.filter(vault=w.vault, agent=w.budgy).exists()


def test_approving_without_a_passkey_is_refused_and_changes_nothing(w):
    req = _file(w.vault, w.budgy, w.beth)
    r = _decide(w.jj, req, step_up=False)
    assert r.status_code == 403 and r.json().get("error") == "step_up_required"
    req.refresh_from_db()
    assert req.status == ApprovalRequest.PENDING and not VaultAttachment.objects.exists()


def test_declining_needs_no_passkey(w):
    req = _file(w.vault, w.budgy, w.beth)
    assert _decide(w.jj, req, step_up=False, decision="decline").json()["status"] == "declined"


def test_a_slack_or_alert_decision_cannot_approve_it(w):
    req = _file(w.vault, w.budgy, w.beth)
    for via in ("slack", "alert"):
        with pytest.raises(services.ApprovalError) as exc:
            services.decide(req.pk, w.jj, "approve", via=via)
        assert exc.value.status == 403 and "passkey" in str(exc.value)
    assert not VaultAttachment.objects.exists()


def test_an_agent_outside_the_vaults_tree_fails_with_nothing_changed(w):
    req = _file(w.connect_vault, w.budgy, w.beth)     # budgy is in dimagi, above the vault's workspace
    r = _decide(w.jj, req, step_up=True)
    assert r.json()["status"] == "failed" and "not connect or a workspace below it" in r.json()["error"]
    assert not VaultAttachment.objects.exists()
