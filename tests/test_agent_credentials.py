"""Agent credentials in canopy-web — values write-only, readable only by a runner.

Spec: docs/superpowers/specs/2026-09-05-agent-credentials-design.md

The goal this serves, stated by Jonathan on 2026-09-05: *someone could plausibly
create a completely new agent without direct access to the cloud box or 1Password.*
1Password is the remaining barrier — a vault you must be granted — so canopy-web
becomes the store and 1Password becomes an import source and a per-secret fallback.

The failure that motivated it is not hypothetical: a from-scratch cloud rebuild the
same day found ACE's mailbox dead since 2026-05-01, under an OAuth client its own
config warns against, and seeing that required SSH-ing to a box and running
`gog auth list`. Credentials that live only in a vault and a keyring are
credentials nobody is watching — which is why `status` matters as much as `set`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SECRET = "op-style-value-not-a-real-token"


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws,
        # The DECLARATION. Shape lives in the agent's runtime.yaml and reaches
        # canopy-web as the registry's secret refs; canopy-web stores VALUES and
        # never re-declares shape (that duplication is what produced the
        # gog_client drift — three copies, two wrong).
        runtime_secrets=["canopy-pat", "gog-token", "nova-api-key"],
    )
    runner = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    # The assignment is what makes this runner one the agent's work can reach —
    # and it is the gate `resolve` checks. A paired runner with no assignment
    # gets nothing, which is the point: plaintext follows routing, not ownership.
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    client.force_login(jj)
    return {"client": client, "agent": agent, "runner": runner, "user": jj, "ws": ws}


def _put(client, values):
    return client.put(
        "/api/agents/ace/credentials",
        data={"values": values}, content_type="application/json",
    )


# --- write-only ----------------------------------------------------------------

def test_a_value_round_trips_only_through_the_runner_route(fleet):
    assert _put(fleet["client"], {"nova-api-key": SECRET}).status_code == 200

    status = fleet["client"].get("/api/agents/ace/credentials/status").json()
    body = str(status)
    assert SECRET not in body, "the status route leaked a value"
    assert any(r["name"] == "nova-api-key" and r["set"] for r in status)


def test_the_stored_column_is_ciphertext_not_plaintext(fleet):
    """Encrypted at rest, asserted against the column rather than trusted."""
    from apps.agents.models import AgentCredential

    _put(fleet["client"], {"nova-api-key": SECRET})
    row = AgentCredential.objects.get(agent=fleet["agent"], name="nova-api-key")
    assert row.value_enc and row.value_enc != SECRET
    assert SECRET not in row.value_enc


def test_no_browser_route_returns_a_value(fleet):
    """Walked, not eyeballed: a future route must not quietly become a leak."""
    _put(fleet["client"], {"nova-api-key": SECRET})
    for path in (
        "/api/agents/ace/credentials/status",
        "/api/agents/ace/runtime",
        "/api/agents/ace",
    ):
        res = fleet["client"].get(path)
        if res.status_code == 200:
            assert SECRET not in res.content.decode(), f"{path} leaked a value"


# --- status is derived from the declaration ------------------------------------

def test_status_lists_every_declared_ref_including_the_unset_ones(fleet):
    _put(fleet["client"], {"canopy-pat": SECRET})
    rows = {r["name"]: r for r in fleet["client"].get("/api/agents/ace/credentials/status").json()}

    assert set(rows) >= {"canopy-pat", "gog-token", "nova-api-key"}
    assert rows["canopy-pat"]["set"] is True
    assert rows["gog-token"]["set"] is False, "an unset declared ref must still appear"
    assert rows["canopy-pat"]["updated_by_email"] == "jj@dimagi.com"


def test_status_shows_where_each_value_comes_from(fleet):
    """The divergence guard. During migration a secret can exist in BOTH stores
    and the box silently prefers canopy-web, so the screen must say which is
    live rather than only that something is set."""
    _put(fleet["client"], {"canopy-pat": SECRET})
    rows = {r["name"]: r for r in fleet["client"].get("/api/agents/ace/credentials/status").json()}
    assert rows["canopy-pat"]["source"] == "canopy-web"
    assert rows["gog-token"]["source"] == "unset"


def test_a_stored_value_nobody_declares_is_surfaced_not_hidden(fleet):
    """An orphan — declared once, since removed from runtime.yaml. Hiding it
    leaves a live secret nothing accounts for."""
    _put(fleet["client"], {"retired-thing": SECRET})
    rows = {r["name"]: r for r in fleet["client"].get("/api/agents/ace/credentials/status").json()}
    assert rows["retired-thing"]["set"] is True
    assert rows["retired-thing"]["declared"] is False


def test_an_agent_that_declares_nothing_shows_nothing_rather_than_guessing(fleet):
    Agent.objects.filter(slug="ace").update(runtime_secrets=[])
    assert fleet["client"].get("/api/agents/ace/credentials/status").json() == []


# --- writes ---------------------------------------------------------------------

def test_a_second_write_rotates_in_place(fleet):
    from apps.agents.models import AgentCredential

    _put(fleet["client"], {"canopy-pat": "first"})
    _put(fleet["client"], {"canopy-pat": "second"})
    assert AgentCredential.objects.filter(agent=fleet["agent"], name="canopy-pat").count() == 1


def test_an_omitted_ref_is_untouched(fleet):
    """Non-clobbering, like RunnerCredentialIn: setting one secret must not wipe
    the others, or a single-field edit becomes a fleet outage."""
    _put(fleet["client"], {"canopy-pat": "a", "nova-api-key": "b"})
    _put(fleet["client"], {"canopy-pat": "c"})
    rows = {r["name"]: r for r in fleet["client"].get("/api/agents/ace/credentials/status").json()}
    assert rows["nova-api-key"]["set"] is True


def test_a_blank_value_is_rejected_rather_than_stored(fleet):
    """"" is how a UI says "I didn't type anything". Storing it would make a
    declared ref read as SET while resolving to nothing."""
    assert _put(fleet["client"], {"canopy-pat": "   "}).status_code == 422


def test_delete_removes_the_slot(fleet):
    _put(fleet["client"], {"canopy-pat": SECRET})
    assert fleet["client"].delete("/api/agents/ace/credentials/canopy-pat").status_code == 200
    rows = {r["name"]: r for r in fleet["client"].get("/api/agents/ace/credentials/status").json()}
    assert rows["canopy-pat"]["set"] is False


# --- resolve: the one plaintext route ------------------------------------------

def test_a_paired_runner_resolves_the_values(fleet):
    _put(fleet["client"], {"canopy-pat": SECRET})
    # A FRESH client: a runner carries a bearer and no session cookie. Reusing
    # the logged-in client would let session auth answer and prove nothing about
    # the bearer path.
    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    assert res.json()["values"]["canopy-pat"] == SECRET


def test_resolve_refuses_a_plain_browser_session(fleet):
    """The browser is never allowed plaintext, even for the owner — that is what
    makes 'write-only' a property of the system rather than of the UI."""
    _put(fleet["client"], {"canopy-pat": SECRET})
    res = fleet["client"].get("/api/agents/ace/credentials/resolve")
    assert res.status_code in (401, 403)
    assert SECRET not in res.content.decode()


def test_resolve_refuses_another_tenants_caller(fleet):
    other = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    _put(fleet["client"], {"canopy-pat": SECRET})
    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(other)}",
    )
    assert res.status_code in (401, 403, 404)
    assert SECRET not in res.content.decode()


def test_a_resolve_is_recorded_so_a_credential_read_is_never_silent(fleet):
    from apps.events.models import Event

    _put(fleet["client"], {"canopy-pat": SECRET})
    Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert Event.objects.filter(kind__icontains="credential").exists()


# --- tenancy --------------------------------------------------------------------

def test_a_non_member_can_neither_read_nor_write(client):
    owner = get_user_model().objects.create_user(username="own", email="own@dimagi.com")
    ws = Workspace.objects.create(slug="private", display_name="Private", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="Secret", workspace=ws, runtime_secrets=["x"])
    client.force_login(get_user_model().objects.create_user(username="m", email="m@dimagi.com"))

    assert client.get("/api/agents/secret/credentials/status").status_code == 404
    assert client.put(
        "/api/agents/secret/credentials",
        data={"values": {"x": "v"}}, content_type="application/json",
    ).status_code == 404


def _pat_for(user) -> str:
    """A personal token for `user` — the same credential a runner authenticates
    with, which is what makes `resolve`'s gate the runner gate and not a new one."""
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw
