"""The vault an agent's secrets live in — custodied here, RESOLVED on the runner.

Jonathan, 2026-09-06: *the service account and vault should be used on the
runner… canopy-web should just store what it needs or to send to the runner.*

A first version made canopy-web the resolver — it held the key, shelled out to
`op`, and stored all 45 values. That turns canopy-web into a second copy of every
credential, free to drift from the vault and worth attacking for the whole set.
The runner already has 1Password access and already resolves secrets there; what
it lacked was WHICH vault per agent (it derived `Agent-<Slug>` in bash) and a key
scoped to it.

So: canopy-web stores the pair and hands it to the runner over the one route that
already carries a plaintext gate. The per-agent scoping was Jonathan's explicit
choice over a fleet-wide token — one key that reads every vault makes canopy-web
worth attacking for every agent at once.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents import vault_import as vi
from apps.common.encryption import encrypt_secret
from apps.agents.models import Agent
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
        name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj}


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


def test_a_non_member_cannot_set_a_vault_or_import(client):
    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    ws = Workspace.objects.create(slug="p", display_name="P", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="S", workspace=ws, runtime_secrets=["x"])
    client.force_login(get_user_model().objects.create_user(username="m", email="m@dimagi.com"))

    assert client.put(
        "/api/agents/secret/vault", data={"vault": "V"}, content_type="application/json",
    ).status_code == 404


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
        AgentIn(slug="ace", name="ACE", runtime_sources={"x": {"op": "op://V/i/f"}}),
        workspace=fleet["agent"].workspace,
    )
    fleet["agent"].refresh_from_db()
    assert fleet["agent"].runtime_sources == {"x": {"op": "op://V/i/f"}}


def test_vault_status_says_how_much_is_locatable(fleet):
    """An import that returns "45 skipped" has two completely different causes —
    the vault is empty, or this deployment was never told where anything lives —
    and nothing on the screen could tell them apart."""
    v = fleet["client"].get("/api/agents/ace/vault").json()
    assert v["declared"] == len(DECLARED)
    # local_only and the undeclared-source ref are not locatable.
    assert v["locatable"] == 3


def test_an_agent_with_no_source_map_reports_zero_locatable(fleet):
    Agent.objects.filter(slug="ace").update(runtime_sources={})
    v = fleet["client"].get("/api/agents/ace/vault").json()
    assert v["declared"] > 0 and v["locatable"] == 0


# --- the runner is what resolves -----------------------------------------------

def test_a_runner_gets_the_vault_and_key_with_the_values(fleet):
    """One route, one gate. The runner needs the vault config and any stored
    value in the same breath, and a second plaintext route would be a second
    boundary to keep correct."""
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")
    _put(fleet["client"], {"gog-token": "minted-in-the-browser"})

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["op_vault"] == "Agent-Ace"
    assert body["op_sa_token"] == "ops_tok"
    assert body["values"]["gog-token"] == "minted-in-the-browser"


def test_the_browser_never_sees_the_service_key(fleet):
    """`resolve` is bearer-only; the vault status route reports key_set and never
    the key itself."""
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")

    assert "ops_tok" not in fleet["client"].get("/api/agents/ace/vault").content.decode()
    denied = fleet["client"].get("/api/agents/ace/credentials/resolve")
    assert denied.status_code in (401, 403)
    assert "ops_tok" not in denied.content.decode()


def test_an_agent_with_no_key_resolves_to_empty_not_an_error(fleet):
    """The box falls back to the runner-wide token, so an unconfigured agent must
    keep working exactly as it did before any of this existed."""
    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    assert res.json()["op_sa_token"] == ""


def test_canopy_web_stores_nothing_it_was_not_given(fleet):
    """The whole correction. There is no path here that populates credentials
    from the vault — what canopy-web holds is only what someone or something
    explicitly wrote to it (the browser mint, a paste)."""
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")
    rows = fleet["client"].get("/api/agents/ace/credentials/status").json()
    assert [r["name"] for r in rows if r["set"]] == []


def _set_vault(client, **body):
    return client.put("/api/agents/ace/vault", data=body, content_type="application/json")


def _put(client, values):
    return client.put(
        "/api/agents/ace/credentials",
        data={"values": values}, content_type="application/json",
    )


def _pat_for(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


# --- the explicit-only runner ---------------------------------------------------

def test_a_disabled_assignment_still_gets_credentials(fleet):
    """The explicit-only runner. Jonathan, 2026-09-06: "I don't want it to fire
    ace commands as a fall back, only if we explicit trigger it."

    `enabled=False` takes a runner out of the automatic rotation, but
    claim_next_turn checks `pinned_runner` BEFORE any assignment lookup, so a
    pinned turn still lands there. Refusing it credentials would let the turn
    arrive and then fail at the far end of a long round trip, which reads as a
    broken agent rather than a routing rule."""
    from apps.harness.models import RunnerAssignment

    RunnerAssignment.objects.filter(agent=fleet["agent"]).update(enabled=False)
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    assert res.json()["op_vault"] == "Agent-Ace"


def test_no_assignment_at_all_is_still_refused(fleet):
    """Disabling loosens automatic ROUTING, not trust. A runner this agent's work
    can never be directed at — pinned or otherwise — gets nothing."""
    from apps.harness.models import RunnerAssignment

    RunnerAssignment.objects.filter(agent=fleet["agent"]).delete()
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 403


# ---- the tenant's SHARED vault (spec 2026-09-07) ------------------------------
#
# An agent's own secrets live in Agent-<Slug>; the ones EVERY agent in a tenant
# needs — the shared gog OAuth clients — live in a shared vault. The box used to
# compile that vault's name in as "Canopy-Shared" and read it with whatever key
# was already exported, which is the per-agent key. Both halves are wrong:
# the name is right for exactly one tenant, and the key structurally cannot read
# it (Agent.op_vault, 2026-09-06 — a per-agent key is scoped on purpose).


def test_the_tenants_shared_vault_and_key_reach_the_runner(fleet):
    """Both halves ride the SAME plaintext route as the per-agent pair.

    A second route would be a second boundary to keep correct, and the reason
    the per-agent pair rides this one is unchanged for the shared pair."""
    _set_vault(fleet["client"], vault="Agent-Ace", service_key="ops_tok")
    ws = fleet["agent"].workspace
    ws.shared_op_vault = "Canopy-Shared"
    ws.shared_op_sa_token_enc = encrypt_secret("shared_tok")
    ws.save(update_fields=["shared_op_vault", "shared_op_sa_token_enc"])

    res = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["shared_op_vault"] == "Canopy-Shared"
    assert body["shared_op_sa_token"] == "shared_tok"
    # And it has not disturbed the per-agent pair beside it — the two are
    # independent credentials for independent vaults, which is the whole point.
    assert body["op_vault"] == "Agent-Ace"
    assert body["op_sa_token"] == "ops_tok"


def test_two_tenants_get_their_own_shared_vault(fleet):
    """The whole point of moving this off a constant: tenants hold different
    values under the same names, so the box must be TOLD, never derive."""
    ws = fleet["agent"].workspace
    ws.shared_op_vault = "Acme-Shared"
    ws.shared_op_sa_token_enc = encrypt_secret("acme_tok")
    ws.save(update_fields=["shared_op_vault", "shared_op_sa_token_enc"])

    body = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    ).json()
    assert body["shared_op_vault"] == "Acme-Shared"
    assert body["shared_op_sa_token"] == "acme_tok"


def test_an_unconfigured_tenant_resolves_to_empty_not_an_error(fleet):
    """Blank-safe, exactly like op_sa_token above: the box falls back to its
    compiled-in default, so every existing deployment behaves as it does today
    and this ships without a flag day."""
    body = Client().get(
        "/api/agents/ace/credentials/resolve",
        HTTP_AUTHORIZATION=f"Bearer {_pat_for(fleet['user'])}",
    ).json()
    assert body["shared_op_vault"] == ""
    assert body["shared_op_sa_token"] == ""


def test_the_browser_never_sees_the_shared_key(fleet):
    """Same boundary as the per-agent key — a shared key is still a key."""
    ws = fleet["agent"].workspace
    ws.shared_op_sa_token_enc = encrypt_secret("shared_tok")
    ws.save(update_fields=["shared_op_sa_token_enc"])

    denied = fleet["client"].get("/api/agents/ace/credentials/resolve")
    assert denied.status_code in (401, 403)
    assert "shared_tok" not in denied.content.decode()
    assert "shared_tok" not in fleet["client"].get("/api/agents/ace/vault").content.decode()


def test_the_shared_key_is_encrypted_at_rest(fleet):
    ws = fleet["agent"].workspace
    ws.shared_op_sa_token_enc = encrypt_secret("shared_tok")
    ws.save(update_fields=["shared_op_sa_token_enc"])
    ws.refresh_from_db()
    assert ws.shared_op_sa_token_enc and "shared_tok" not in ws.shared_op_sa_token_enc
