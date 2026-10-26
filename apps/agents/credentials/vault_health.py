"""Where an agent's secrets live, and what is missing — THE vault verdict.

One answer for every screen that asks: the setup checklist's vault step
(`setup._vault`) and the workspace health dashboard (`health.agent_row`) both
read `vault_health`, so they cannot disagree about whether an agent is done
moving off 1Password.

Each declared ref (`Agent.runtime_secrets`) is classified against what is
actually stored in canopy-web (`AgentCredential`) and where its vault declaration
says it lives (`runtime_sources`, the same `vault_import.plan_import` the importer
uses):

- ``stored``    — a value is in canopy-web.
- ``literal``   — the declaration carries it as a plain value (a base URL); no
                  secret to move.
- ``local``     — minted per machine (`local_only`); never in any vault.
- ``1password`` — only an ``op://`` reference: it lives in 1Password.
- ``missing``   — no value here, and the declaration does not say where it lives.

An agent resolving from canopy-web (`credential_source`) is fully moved when no
ref is ``1password`` or ``missing``. One resolving from 1Password needs its vault
and the key a runner reads it with.
"""
from __future__ import annotations

from apps.harness import device_keys

from ..models import Agent, AgentCredential
from . import vault_import, zk

STORED, LITERAL, LOCAL, ONEPASSWORD, MISSING = "stored", "literal", "local", "1password", "missing"

#: The agent's own vault state, in the words the dashboard shows.
MOVED, MOVING, ON_1PASSWORD, NO_SECRETS = "canopy-web", "moving", "1password", "none"


def classify(agent: Agent, stored: set[str] | None = None) -> dict[str, str]:
    """{ref: class} for every declared ref, in declaration order."""
    if stored is None:
        stored = set(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True))
    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    out: dict[str, str] = {}
    for item in vault_import.plan_import(declared, agent.runtime_sources or {}):
        if item.name in stored:
            out[item.name] = STORED
        elif item.kind == "value":
            out[item.name] = LITERAL
        elif item.kind == "op":
            out[item.name] = ONEPASSWORD
        elif "local-only" in item.reason:
            out[item.name] = LOCAL
        else:
            out[item.name] = MISSING
    return out


def machine_keys_waiting(agent: Agent, facts=None) -> dict[str, str]:
    """Runners that would run this agent but cannot open its zero-knowledge
    values yet (`zk.device_waits`), or whose new ECDH key awaits approval, by name →
    the device key a member's passkey touch lets in. `facts`: `device_keys.serving_facts`."""
    if not agent.zk_enabled:
        return {}

    facts = facts or device_keys.serving_facts([agent])
    eligible = zk.eligible_device_keys(agent, facts)
    blocked = zk.devices_waiting(agent, eligible)
    waiting = {k.runner.name: str(k.pk) for k in eligible if k.pk in blocked}
    waiting.update({k.runner.name: str(k.pk) for k in device_keys.keys_for_agent(agent, facts) if k.ecdh_waiting})
    return dict(sorted(waiting.items()))


def machines_waiting(agent: Agent, facts=None) -> list[str]:
    """The names of `machine_keys_waiting`."""
    return list(machine_keys_waiting(agent, facts))


def vault_health(agent: Agent, facts=None) -> dict:
    refs = classify(agent)
    by_class: dict[str, list[str]] = {}
    for name, cls in refs.items():
        by_class.setdefault(cls, []).append(name)
    on_canopy = agent.credential_source == Agent.CANOPY_WEB
    leftover = by_class.get(ONEPASSWORD, []) + by_class.get(MISSING, [])
    if on_canopy:
        state = MOVING if leftover else MOVED
    elif not refs and not agent.op_vault:
        state = NO_SECRETS
    else:
        state = ON_1PASSWORD
    return {
        "source": agent.credential_source,
        "state": state,
        "declared": len(refs),
        "refs": [{"name": n, "status": c} for n, c in refs.items()],
        "stored": by_class.get(STORED, []),
        "in_1password": by_class.get(ONEPASSWORD, []),
        "missing": by_class.get(MISSING, []),
        "op_vault": agent.op_vault,
        "op_key_set": bool(agent.op_sa_token_enc),
        "zero_knowledge": agent.zk_enabled,
        "machines_waiting": machines_waiting(agent, facts) if on_canopy else [],
    }
