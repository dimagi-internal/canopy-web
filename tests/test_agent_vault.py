"""An agent's declared secrets and the canopy vault they resolve from.

`runtime_sources` is the agent's declaration of where each ref comes from (a literal
value, or minted per machine); the secrets themselves live in the canopy vault and
reach a runner over the one route that carries a plaintext gate. 1Password is gone:
an `op` pointer in a declaration leads nowhere and is dropped on the way in.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.credentials import vault_health as vh
from apps.agents.models import Agent
from apps.passkeys.testing import give_vault_key, grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.device_proof import accept_device_proof

pytestmark = pytest.mark.django_db

SOURCES = {
    "canopy-pat": {},
    "ace-hq-base-url": {"value": "https://www.commcarehq.org"},
    "ace-web-pat-token": {"local_only": True},
}
DECLARED = list(SOURCES) + ["undeclared-source"]


# --- classifying (pure) --------------------------------------------------------

def test_classify_reads_the_declaration_not_a_convention(fleet):
    got = vh.classify(fleet["agent"], stored=set())
    assert got == {
        "canopy-pat": "missing",
        "ace-hq-base-url": "literal",   # a base URL is not a secret
        "ace-web-pat-token": "local",   # minted per human: no vault copy exists
        "undeclared-source": "missing",
    }
    assert vh.classify(fleet["agent"], stored={"canopy-pat"})["canopy-pat"] == "stored"


def test_an_op_only_declaration_is_missing_not_an_error(fleet):
    """An older plugin still publishes `{"op": "op://..."}`; the pointer is dead, so
    the ref is simply one the vault has yet to receive."""
    Agent.objects.filter(slug="ace").update(
        runtime_secrets=["old"], runtime_sources={"old": {"op": "op://Agent-Ace/old/credential"}})
    agent = Agent.objects.get(slug="ace")
    assert vh.classify(agent, stored=set()) == {"old": "missing"}
    health = vh.vault_health(agent)
    assert health["state"] == "missing" and health["missing"] == ["old"]
    assert "in_1password" not in health and "source" not in health


# --- resolving -----------------------------------------------------------------

@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws,
        runtime_secrets=DECLARED, runtime_sources=SOURCES,
    )
    # A live runner this agent routes to — the gate `resolve` checks. Without the
    # ASSIGNMENT a paired runner gets nothing, which is the point: plaintext
    # follows routing, not ownership.
    from django.utils import timezone

    from apps.harness.models import Runner, RunnerAssignment

    runner = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    give_vault_key(jj)  # someone to seal a stored value to (the agent vault is zero-knowledge)
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj}


def test_runtime_sources_survives_a_plugin_reupsert(fleet):
    """The registry fields are written only when PRESENT. An agent plugin
    re-upserts itself on every sync with these omitted, and a plain default would
    clobber the source map back to empty on each heartbeat — which would silently
    turn every subsequent import into a 45-way skip."""
    from apps.agents import services as svc
    from apps.agents.schemas import AgentIn

    svc.upsert_agent(AgentIn(slug="ace", name="ACE"), workspace=fleet["agent"].workspace)
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].runtime_sources == SOURCES


def test_runtime_sources_can_be_pushed_with_the_agent(fleet):
    from apps.agents import services as svc
    from apps.agents.schemas import AgentIn

    svc.upsert_agent(
        AgentIn(slug="ace", name="ACE", runtime_sources={"x": {"value": "https://x"}}),
        workspace=fleet["agent"].workspace,
    )
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].runtime_sources == {"x": {"value": "https://x"}}


def test_upsert_strips_op_pointers_from_runtime_sources(fleet):
    """An older agent plugin still publishes `op://` pointers. They are dropped on the
    way in; whatever else the entry carries (a literal, `local_only`) is kept."""
    from apps.agents import services as svc
    from apps.agents.schemas import AgentIn

    sources = {
        "gog-token": {"op": "op://Agent-Ace/gog-token/credential"},
        "base-url": {"value": "https://x", "op": "op://Agent-Ace/base-url/credential"},
        "mint": {"local_only": True},
    }
    svc.upsert_agent(AgentIn(slug="ace", name="ACE", runtime_sources=sources),
                     workspace=fleet["agent"].workspace)
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].runtime_sources == {
        "gog-token": {}, "base-url": {"value": "https://x"}, "mint": {"local_only": True}}
    assert "op://" not in str(fleet["agent"].runtime_sources)


# --- the runner is what resolves -----------------------------------------------

def test_a_runner_gets_the_stored_values(fleet, monkeypatch):
    """One route, one gate: any stored value comes back sealed to the agent's readers."""
    _put(fleet["client"], {"gog-token": "minted-in-the-browser"})
    accept_device_proof(monkeypatch)

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    body = res.json()
    assert "op_vault" not in body and "op_sa_token" not in body
    assert body["values"]["gog-token"].startswith("zks1:")
    assert "minted-in-the-browser" not in res.content.decode()


def test_the_browser_session_cannot_resolve(fleet):
    """`resolve` is bearer-only."""
    denied = fleet["client"].get("/api/agents/ace/credentials/resolve")
    assert denied.status_code in (401, 403)


def test_canopy_web_stores_nothing_it_was_not_given(fleet):
    """What canopy-web holds is only what someone or something explicitly wrote to
    it (the browser mint, a paste); declaring a ref stores nothing."""
    rows = fleet["client"].get("/api/agents/ace/credentials/status").json()
    assert [r["name"] for r in rows if r["set"]] == []


def _put(client, values):
    grant_step_up(client)  # a browser write needs a passkey step-up (tests/test_agent_credential_stepup.py)
    return client.put(
        "/api/agents/ace/credentials",
        data={"values": values}, content_type="application/json",
    )


def _pat_for(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


# --- the explicit-only runner ---------------------------------------------------

def test_a_disabled_assignment_still_gets_credentials(fleet, monkeypatch):
    """The explicit-only runner. Jonathan, 2026-09-06: "I don't want it to fire
    ace commands as a fall back, only if we explicit trigger it."

    `enabled=False` takes a runner out of the automatic rotation, but
    claim_next_turn checks `pinned_runner` BEFORE any assignment lookup, so a
    pinned turn still lands there. Refusing it credentials would let the turn
    arrive and then fail at the far end of a long round trip, which reads as a
    broken agent rather than a routing rule."""
    from apps.harness.models import RunnerAssignment

    RunnerAssignment.objects.filter(agent=fleet["agent"]).update(enabled=False)
    _put(fleet["client"], {"gog-token": "minted-in-the-browser"})
    accept_device_proof(monkeypatch)

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200


def test_no_assignment_at_all_is_still_refused(fleet, monkeypatch):
    """Disabling loosens automatic ROUTING, not trust. A runner this agent's work
    can never be directed at — pinned or otherwise — gets nothing as a RUNNER.

    Its pairing human may still resolve as the agent's owner/admin (canopy#850's
    admin path) — but that is a different principal, audited as `via: admin`,
    and a non-admin with the same unassigned box is refused outright."""
    from django.utils import timezone

    from apps.events.models import Event
    from apps.harness.models import Runner, RunnerAssignment

    RunnerAssignment.objects.filter(agent=fleet["agent"]).delete()
    _put(fleet["client"], {"gog-token": "minted-in-the-browser"})

    member = get_user_model().objects.create_user(username="mem", email="mem@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["agent"].workspace, user=member,
                                       role=WorkspaceMembership.EDITOR)
    Runner.objects.create(name="mem-box", kind=Runner.CLOUD, owner=member, status=Runner.ONLINE,
                          last_heartbeat_at=timezone.now(), capabilities={})
    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(member)}",
    )
    assert res.status_code == 403
    assert "minted-in-the-browser" not in res.content.decode()

    accept_device_proof(monkeypatch)
    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    ev = Event.objects.filter(kind="agent.credentials.resolved").latest("id")
    assert ev.payload["via"] == "admin"
