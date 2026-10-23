"""Whether a capability of an agent may serve a host's sensitive data (PHI) — an
interface setting, `data: phi` (spec 2026-10-22-phi-agents-on-zdr-runners §5).

A capability says `data: phi` on the published interface. That is accepted only

* **when it is structurally true** (`structural_refusals`, asked by
  `interface.parse`): a phi capability reaches its host through canopy's gateway
  (`sites:` is required), and its tools may be only the gateway's site tools,
  `current_page`, `who_is_asking` and read-only local tools — no shell, no
  writes, nothing that leaves the box (Drive, email, memory, Slack, the web).
  Anything else is a 422 naming it;
* **from the agent's owner or an admin** (`publish_interface`), from the browser or
  a token, like any other interface change. Every interface save is a setting
  change, audited with its before and after (`apps/events/audit.py`).

What it buys, and what reads it:

* **confinement for everyone** — a turn in a phi capability, or any turn in a
  `zdr` conversation, runs the capability's confined profile whoever asked
  (`access.decide`, `phi_capability`);
* **a ZDR conversation's host data** — the gateway calls a host for a `zdr`
  conversation only from a phi capability (`host_gateway`);
* **canopy vouches for it to the host** — the gateway's `Canopy-Execution`
  statement carries the setting (`as_claim`), and the host decides whether to
  accept it.
"""
from __future__ import annotations

import re

PHI = "phi"
DATA_VALUES = frozenset({PHI})

#: Local tools a phi capability may hold: they read the agent's own checkout.
READ_ONLY_LOCAL = frozenset({"Read", "Grep", "Glob", "Skill", "ToolSearch", "TodoWrite"})
#: canopy's own tools a phi capability may hold (any MCP server prefix ending in
#: `canopy-web__`, as interface tool patterns are written).
_CANOPY_TOOL = re.compile(
    r"^mcp__(?:\*|[A-Za-z0-9_.-]*?)canopy-web__(?:current_page|who_is_asking|site_tools|site_call)$")


def structural_refusals(name: str, cap: dict) -> list[str]:
    """Why `cap` cannot honestly be declared PHI-safe — each named. Empty: it can."""
    out = []
    if not cap.get("sites"):
        out.append(f"{name}: a `data: phi` capability must name `sites:` — a host's data "
                   "reaches it only through canopy's gateway")
    bad = [t for t in cap.get("tools") or []
           if t not in READ_ONLY_LOCAL and not _CANOPY_TOOL.match(t)]
    if bad:
        out.append(f"{name}: a `data: phi` capability may hold only the gateway's site tools, "
                   f"current_page, who_is_asking and {', '.join(sorted(READ_ONLY_LOCAL))}; "
                   f"refused: {', '.join(bad)}")
    if cap.get("bash"):
        out.append(f"{name}: a `data: phi` capability may run no shell (`bash:` must be empty)")
    if cap.get("write_paths"):
        out.append(f"{name}: a `data: phi` capability may write nothing (`write_paths:` must "
                   "be empty)")
    return out


def phi_capabilities(iface: dict | None) -> list[str]:
    return sorted(n for n, c in ((iface or {}).get("capabilities") or {}).items()
                  if (c or {}).get("data") == PHI)


def is_phi(agent, capability: str | None) -> bool:
    if not capability:
        return False
    cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(capability)
    return bool(cap) and cap.get("data") == PHI


def as_claim(agent, capability: str | None) -> dict | None:
    """The setting as canopy states it to a host (`Canopy-Execution`): who last
    published the interface that holds it, and when. None when the capability
    is not `data: phi`."""
    if not is_phi(agent, capability):
        return None
    by = agent.interface_published_by
    at = agent.interface_published_at
    return {"data": PHI, "capability": capability,
            "declared_by": getattr(by, "email", "") if by is not None else "",
            "declared_at": at.isoformat() if at else ""}
