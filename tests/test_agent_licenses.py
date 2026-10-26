"""Licenses: who uses an agent locally, and what that lets them read (canopy-web#1704).

The caller list decides who MAY; the license records who DOES. It is issued on
first local use when `access.decide` gives the person the whole agent, revoked per
person without editing the rule, and it is the third principal of
`/credentials/resolve` — only during the holder's own open local turn.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent, AgentAdmin, AgentCredential, AgentCredentialRead, AgentLicense
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# What canopy-web holds and hands out: ciphertext (the agent vault is zero-knowledge).
SECRET = "zks1:nova-ciphertext"


def _user(name, domain="dimagi.com"):
    return get_user_model().objects.create_user(username=name, email=f"{name}@{domain}")


def _session(user):
    c = Client()
    c.force_login(user)
    return c


def _bearer(user):
    raw, _row = PersonalToken.create_for_user(user=user, label="t")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


@pytest.fixture
def world():
    jj = _user("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    # vic is a viewer from a partner domain: `member@dimagi.com` does not name them, so
    # the caller list offers them only the confined `ask` door. (A dimagi.com viewer
    # WOULD get the whole agent from that rule — rule 3 comes before the editor tier.)
    aath, sarvesh = _user("aath"), _user("sarvesh")
    vic, hal = _user("vic", "partner.org"), _user("hal", "dimagi-ai.com")
    for u, role in ((aath, "editor"), (vic, "viewer"), (sarvesh, "editor"), (hal, "editor")):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
    agent = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws, owner=jj, runtime_secrets=["nova-api-key"],
        # Who may read, not the device layer: a bearer-only read, which a canopy-vault
        # agent would refuse without a device proof (tests/test_canopy_vault_zk.py).
        credential_source=Agent.ONEPASSWORD, zk_enabled=True,
        interface={"version": 1, "full": ["member@dimagi.com"], "capabilities": {
            "ask": {"callers": ["member", "contact"], "tools": ["Read"]}}},
    )
    AgentCredential.objects.create(agent=agent, name="nova-api-key", value_zk=SECRET, kv=0)
    AgentAdmin.objects.create(agent=agent, user=sarvesh, granted_by=jj)
    return {"agent": agent, "owner": jj, "aath": aath, "viewer": vic, "admin": sarvesh, "hal": hal}


def _open(user, session="s1"):
    c = _session(user)
    ws = c.post("/api/private-runners/", data={"name": f"{user.username}-mbp"},
                content_type="application/json").json()["id"]
    return c.post("/api/harness/local-turns/", data={
        "agent": "ace", "private_runner": ws, "claude_session_id": session,
    }, content_type="application/json")


def _resolve(user, turn_id=None):
    headers = _bearer(user)
    if turn_id:
        headers["HTTP_X_CANOPY_TURN_ID"] = str(turn_id)
    return Client().get("/api/agents/ace/credentials/resolve", **headers)


# --- issuing ----------------------------------------------------------------------

def test_a_full_rule_member_is_licensed_on_first_use(world):
    res = _open(world["aath"])
    assert res.status_code == 201, res.content
    lic = AgentLicense.objects.get(agent=world["agent"], user=world["aath"])
    assert lic.status == AgentLicense.ACTIVE and lic.basis == "full:member@dimagi.com"
    assert lic.last_private_runner.name == "aath-mbp"
    turn = Turn.objects.get(pk=res.json()["turn_id"])
    assert turn.origin_ref["license"]["id"] == lic.pk


def test_an_editor_with_no_full_rule_is_licensed_through_the_editor_tier(world):
    """hal@dimagi-ai.com: an editor the `full:` rule does not name."""
    assert _open(world["hal"]).status_code == 201
    lic = AgentLicense.objects.get(agent=world["agent"], user=world["hal"])
    assert lic.status == AgentLicense.ACTIVE and lic.basis == "editor"


def test_the_owner_holds_one_implicitly(world):
    assert _open(world["owner"]).status_code == 201
    assert AgentLicense.objects.get(user=world["owner"]).basis == "owner"


def test_a_viewer_gets_a_recorded_request_and_no_turn(world):
    res = _open(world["viewer"])
    assert res.status_code == 403 and "admins" in res.json()["detail"]
    lic = AgentLicense.objects.get(user=world["viewer"])
    assert lic.status == AgentLicense.REQUESTED and lic.note
    assert not Turn.objects.filter(origin=Turn.ORIGIN_LOCAL).exists()


# --- revoking ---------------------------------------------------------------------

def test_revocation_beats_the_caller_list(world):
    assert _open(world["aath"]).status_code == 201
    admin = _session(world["admin"])
    res = admin.post(f"/api/agents/ace/licenses/{world['aath'].pk}/revoke",
                     data={"note": "left the project"}, content_type="application/json")
    assert res.status_code == 200 and res.json()["status"] == "revoked"
    again = _open(world["aath"], session="s2")
    assert again.status_code == 403 and "revoked" in again.json()["detail"]


def test_approve_reinstates_but_only_whom_the_caller_list_allows(world):
    admin = _session(world["admin"])
    admin.post(f"/api/agents/ace/licenses/{world['aath'].pk}/revoke", content_type="application/json")
    assert admin.post(f"/api/agents/ace/licenses/{world['aath'].pk}/approve").json()["status"] == "active"
    assert _open(world["aath"], session="s3").status_code == 201
    # A viewer is not reinstated by an admin's say-so: the caller list decides.
    _open(world["viewer"])
    refused = admin.post(f"/api/agents/ace/licenses/{world['viewer'].pk}/approve")
    assert refused.status_code == 409 and "full" in refused.json()["detail"]


def test_only_an_agent_admin_changes_licenses(world):
    _open(world["hal"])
    res = _session(world["aath"]).post(f"/api/agents/ace/licenses/{world['hal'].pk}/revoke",
                                       content_type="application/json")
    assert res.status_code == 403


def test_the_agent_lists_its_license_holders_with_their_private_runners(world):
    _open(world["aath"])
    _open(world["viewer"])
    rows = {r["email"]: r for r in _session(world["aath"]).get("/api/agents/ace/licenses").json()}
    assert rows["aath@dimagi.com"]["status"] == "active"
    assert rows["aath@dimagi.com"]["private_runners"] == ["aath-mbp"]
    assert rows["aath@dimagi.com"]["local_turns"] == 1
    assert rows["vic@partner.org"]["status"] == "requested"


# --- credentials: the license principal ------------------------------------------

def test_a_license_holder_reads_secrets_only_during_their_local_turn(world):
    turn_id = _open(world["aath"]).json()["turn_id"]
    assert _resolve(world["aath"]).status_code == 403            # no turn named
    res = _resolve(world["aath"], turn_id)
    assert res.status_code == 200, res.content
    assert res.json()["values"]["nova-api-key"] == SECRET
    read = AgentCredentialRead.objects.get(name="nova-api-key", user=world["aath"])
    assert read.via == "license" and read.turn_id == turn_id
    assert read.license == AgentLicense.objects.get(user=world["aath"])


def test_someone_elses_turn_or_a_finished_one_admits_nothing(world):
    turn_id = _open(world["aath"]).json()["turn_id"]
    _open(world["hal"])
    assert _resolve(world["hal"], turn_id).status_code == 403   # not their turn
    c = _session(world["aath"])
    c.post(f"/api/harness/turns/{turn_id}/finish", data={"status": "done"},
           content_type="application/json")
    assert _resolve(world["aath"], turn_id).status_code == 403  # turn over


def test_a_revoked_license_stops_reads_mid_turn(world):
    turn_id = _open(world["aath"]).json()["turn_id"]
    assert _resolve(world["aath"], turn_id).status_code == 200
    _session(world["admin"]).post(f"/api/agents/ace/licenses/{world['aath'].pk}/revoke",
                                  content_type="application/json")
    assert _resolve(world["aath"], turn_id).status_code == 403


def test_the_access_check_says_license(world):
    turn_id = _open(world["aath"]).json()["turn_id"]
    headers = {**_bearer(world["aath"]), "HTTP_X_CANOPY_TURN_ID": turn_id}
    body = Client().get("/api/agents/ace/credentials/access", **headers).json()
    assert body["may_resolve"] is True and body["via"] == "license"


def test_a_license_read_withholds_what_is_not_the_agents_tools(world, monkeypatch):
    """Never the owner's GitHub delegation or the agent's own canopy
    login — the holder runs the agent's tools, not the agent."""
    from types import SimpleNamespace

    from apps.agents import api as agents_api

    agent = world["agent"]
    agent.runtime_secrets = ["nova-api-key", "canopy-pat"]
    agent.save(update_fields=["runtime_secrets"])
    AgentCredential.objects.create(agent=agent, name="canopy-pat", value_zk="zks1:agent-own-pat", kv=0)
    monkeypatch.setattr(agents_api.delegations, "delegation_for",
                        lambda a: SimpleNamespace(secret_enc="x"))
    monkeypatch.setattr(agents_api.delegations, "decrypt_secret", lambda s: "owner-github-token")

    turn_id = _open(world["aath"]).json()["turn_id"]
    body = _resolve(world["aath"], turn_id).json()
    assert body["values"] == {"nova-api-key": SECRET}
    assert "github_token" not in body
    # The same agent's admin still gets everything (unchanged path).
    admin = _resolve(world["owner"]).json()
    assert admin["values"]["canopy-pat"] == "zks1:agent-own-pat"
    # The agent's GitHub token is no read's, an admin's included: a turn's only.
    assert "github_token" not in admin


def test_a_license_read_never_gets_the_agents_1password_key(world, monkeypatch):
    """The service-account key reads the whole vault, the agent's canopy-pat included,
    so handing it to a license holder would undo withholding that login."""
    from apps.agents import api as agents_api

    agent = world["agent"]
    Agent.objects.filter(pk=agent.pk).update(credential_source=Agent.ONEPASSWORD)
    monkeypatch.setattr(agents_api.services, "resolve_agent_vault", lambda a: ("Agent-ACE", "ops-sa-key"))
    turn_id = _open(world["aath"]).json()["turn_id"]
    body = _resolve(world["aath"], turn_id).json()
    assert body["op_sa_token"] == "" and body["op_vault"] == "Agent-ACE"
    assert _resolve(world["owner"]).json()["op_sa_token"] == "ops-sa-key"
