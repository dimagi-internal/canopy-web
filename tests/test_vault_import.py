"""Importing an agent's secrets from its own 1Password vault.

The goal (Jonathan, 2026-09-05): *someone could plausibly create a completely new
agent without direct access to the cloud box or 1Password.* The browser mint took
the box out of the mailbox path; this takes the vault out of everything else.

The per-agent key was Jonathan's explicit choice on 2026-09-06 over a single
fleet-wide token. One key that reads every vault is simpler to operate and makes
canopy-web worth attacking for every agent's secrets at once; a scoped key bounds
a compromise to one agent.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents import vault_import as vi
from apps.agents.models import Agent, AgentCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# ACE's real shape, from runtime.yaml (ace#2060).
SOURCES = {
    "canopy-pat": {"op": "op://Agent-Ace/canopy-pat/credential"},
    "gog-token": {"op": "op://Agent-Ace/gog-token/credential"},
    "ace-hq-base-url": {"value": "https://www.commcarehq.org"},
    "ace-web-pat-token": {"local_only": True},
}
DECLARED = list(SOURCES) + ["undeclared-source"]


# --- planning (pure) -----------------------------------------------------------

def test_op_backed_refs_are_fetched_and_literals_are_copied():
    plan = {i.name: i for i in vi.plan_import(DECLARED, SOURCES)}
    assert plan["canopy-pat"].kind == "op"
    assert plan["canopy-pat"].ref == "op://Agent-Ace/canopy-pat/credential"
    # A base URL is not a secret. Reading it from a vault would be a round trip
    # to fetch a constant the declaration already carries.
    assert plan["ace-hq-base-url"].kind == "value"
    assert plan["ace-hq-base-url"].value == "https://www.commcarehq.org"


def test_a_local_only_ref_is_never_invented():
    """Minted per-human on a workstation — there is no vault copy. Fabricating
    one would make the screen claim a value canopy-web does not have."""
    item = {i.name: i for i in vi.plan_import(DECLARED, SOURCES)}["ace-web-pat-token"]
    assert item.kind == "skip"
    assert "local-only" in item.reason


def test_a_ref_with_no_declared_source_is_skipped_not_guessed():
    """The load-bearing one. Deriving op://<vault>/<name>/credential is the
    obvious convention, and for ACE's gog-oauth-client it RESOLVES — to a
    different OAuth app in a different GCP project than the one ACE uses. A
    convention that silently returns the wrong credential is worse than one that
    fails, so an undeclared ref must produce nothing at all."""
    item = {i.name: i for i in vi.plan_import(DECLARED, SOURCES)}["undeclared-source"]
    assert item.kind == "skip"
    assert not item.ref and not item.value


# --- resolving -----------------------------------------------------------------

def test_one_bad_ref_does_not_abandon_the_others():
    """A vault missing one item is the normal state of a half-provisioned agent.
    All-or-nothing would make such an agent unprovisionable."""
    def reader(ref, *, token):
        if "gog-token" in ref:
            raise RuntimeError("isn't an item in the vault")
        return "value-for-" + ref.rsplit("/", 2)[1]

    values, failures = vi.resolve_items(vi.plan_import(DECLARED, SOURCES), token="t", reader=reader)
    assert values["canopy-pat"] == "value-for-canopy-pat"
    assert values["ace-hq-base-url"] == "https://www.commcarehq.org"
    assert [f["name"] for f in failures] == ["gog-token"]
    assert "isn't an item" in failures[0]["error"]


def test_an_empty_read_is_a_failure_not_a_value():
    """Storing "" would show the ref as SET while it resolves to nothing — the
    provisioned-looking-but-dead state this whole effort exists to surface."""
    values, failures = vi.resolve_items(
        vi.plan_import(["canopy-pat"], SOURCES), token="t", reader=lambda ref, *, token: "",
    )
    assert values == {}
    assert failures[0]["error"] == "resolved empty"


def test_the_service_key_never_reaches_a_command_line(monkeypatch):
    """argv is world-readable via /proc. The token goes in the environment."""
    seen = {}

    class Res:
        returncode, stdout, stderr = 0, "v", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env", {})
        return Res()

    monkeypatch.setattr(vi.subprocess, "run", fake_run)
    vi.op_read("op://V/i/f", token="ops_supersecret")
    assert "ops_supersecret" not in " ".join(seen["cmd"])
    assert seen["env"]["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_supersecret"


# --- through the API -----------------------------------------------------------

@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws,
        runtime_secrets=DECLARED, runtime_sources=SOURCES,
    )
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj}


def _set_vault(client, **body):
    return client.put("/api/agents/ace/vault", data=body, content_type="application/json")


def test_the_service_key_is_encrypted_and_never_read_back(fleet):
    assert _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok").status_code == 200
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].op_sa_token_enc and "ops_tok" not in fleet["agent"].op_sa_token_enc

    body = fleet["client"].get("/api/agents/ace/vault").content.decode()
    assert "ops_tok" not in body
    assert '"key_set": true' in body.replace(" ", "").replace('"key_set":true', '"key_set": true')


def test_renaming_the_vault_does_not_wipe_the_key(fleet):
    """A single-field edit becoming a de-provisioning is the shape of outage this
    codebase already fixed once for RunnerCredential."""
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")
    _set_vault(fleet["client"], vault="Agent-Ace-New")
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].op_vault == "Agent-Ace-New"
    assert fleet["agent"].op_sa_token_enc


def test_import_refuses_before_a_key_is_set(fleet):
    assert fleet["client"].post("/api/agents/ace/credentials/import").status_code == 422


def test_import_stores_values_and_reports_what_it_could_not_get(fleet, monkeypatch):
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")

    def reader(ref, *, token):
        if "gog-token" in ref:
            raise RuntimeError("stale ref")
        return "resolved"

    monkeypatch.setattr(vi, "op_read", reader)
    res = fleet["client"].post("/api/agents/ace/credentials/import")
    assert res.status_code == 200
    body = res.json()

    assert "canopy-pat" in body["imported"]
    assert "ace-hq-base-url" in body["imported"]
    assert [f["name"] for f in body["failures"]] == ["gog-token"]
    assert {s["name"] for s in body["skipped"]} == {"ace-web-pat-token", "undeclared-source"}
    assert AgentCredential.objects.filter(agent=fleet["agent"], name="canopy-pat").exists()


def test_an_import_never_returns_a_value_to_the_browser(fleet, monkeypatch):
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")
    monkeypatch.setattr(vi, "op_read", lambda ref, *, token: "SUPERSECRET")
    res = fleet["client"].post("/api/agents/ace/credentials/import")
    assert "SUPERSECRET" not in res.content.decode()


def test_a_non_member_cannot_set_a_vault_or_import(client):
    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    ws = Workspace.objects.create(slug="p", display_name="P", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="S", workspace=ws, runtime_secrets=["x"])
    client.force_login(get_user_model().objects.create_user(username="m", email="m@dimagi.com"))

    assert client.put(
        "/api/agents/secret/vault", data={"vault": "V"}, content_type="application/json",
    ).status_code == 404
    assert client.post("/api/agents/secret/credentials/import").status_code == 404
