"""Helpers for tests that need an agent in a particular state."""
from __future__ import annotations

from apps.agents.credentials import zk_seal
from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.passkeys.testing import grant_step_up


def put_sealed(client, slug: str, values: dict[str, str]):
    """Store `values` as a person's browser does: sealed (`zks1`) to everyone who may read
    the agent, then `PUT …/vault/zk/values` with the ciphertext only — canopy-web takes no
    plaintext (`PUT …/credentials` is 422 `sealed_only`). The sealing runs here, standing
    in for the browser; the route under test sees only ciphertext. Steps up first, as a
    browser write must."""

    agent = Agent.objects.get(slug=slug)
    kv = agent.zk_kv if agent.zk_enabled else 0
    body = {"kv": kv, "values": [{"name": n, "value_zk": zk_seal.seal_to(
        v, agent_id=str(agent.pk), name=n, kv=kv, recipients=zk_seal.recipients_for(agent))}
        for n, v in values.items()]}
    grant_step_up(client)
    return client.put(f"/api/agents/{slug}/vault/zk/values", data=body, content_type="application/json")


def admit_contacts(agent) -> None:
    """Publish `full: [contact]` on `agent`, for a test about something OTHER
    than access (email grading, turn modes, widget runs). Since 2026-09-26 an
    agent with no interface refuses contacts, so such a test has to say, in
    the open, that this agent lets them in."""

    agent.interface = parse({"full": ["contact"]})
    agent.save(update_fields=["interface"])
