"""Where an agent's secrets live, and what is missing — THE vault verdict.

One answer for every screen that asks: the setup checklist's vault step
(`setup._vault`) and the workspace health dashboard (`health.agent_row`) both
read `vault_health`, so they cannot disagree about whether an agent's declared
secrets are all in the vault.

Each declared ref (`Agent.runtime_secrets`) is classified against what is
actually stored in canopy-web (`AgentCredential`) and what its declaration says
(`runtime_sources`):

- ``stored``    — a value is in canopy-web.
- ``literal``   — the declaration carries it as a plain value (a base URL); no
                  secret to move.
- ``local``     — minted per machine (`local_only`); never in any vault.
- ``missing``   — no value here, and the declaration does not say where it lives.

An agent is complete when no ref is ``missing``.
"""
from __future__ import annotations

from apps.harness import device_keys

from ..models import Agent, AgentCredential
from . import zk

STORED, LITERAL, LOCAL, MISSING = "stored", "literal", "local", "missing"

#: The agent's own vault state, in the words the dashboard shows.
MOVED, MOVING = "canopy-web", "moving"


def classify(agent: Agent, stored: set[str] | None = None) -> dict[str, str]:
    """{ref: class} for every declared ref, in declaration order."""
    if stored is None:
        stored = set(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True))
    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    out: dict[str, str] = {}
    sources = agent.runtime_sources or {}
    for name in declared:
        src = sources.get(name)
        src = src if isinstance(src, dict) else {}
        if name in stored:
            out[name] = STORED
        elif "value" in src and not src.get("local_only"):
            # Declared literal — a base URL, an APK version. Not a secret at all.
            out[name] = LITERAL
        elif src.get("local_only"):
            # Minted per-human on a workstation: no vault copy exists.
            out[name] = LOCAL
        else:
            out[name] = MISSING
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
    state = MOVING if by_class.get(MISSING) else MOVED
    return {
        "state": state,
        "declared": len(refs),
        "refs": [{"name": n, "status": c} for n, c in refs.items()],
        "stored": by_class.get(STORED, []),
        "missing": by_class.get(MISSING, []),
        "zero_knowledge": agent.zk_enabled,
        "machines_waiting": machines_waiting(agent, facts),
    }
