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

Two ways to say it, one stored setting: the YAML (`publish_interface`), or the
per-capability switch (`set_capability_phi`), which edits the saved YAML itself
(`with_phi`) and saves it down the same path — so the source an editor next
opens always says what the switch did.

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

import copy
import re

import yaml

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


class SourceEditError(ValueError):
    """The saved YAML is laid out in a way `with_phi` will not edit by line."""


_TOP_CAPABILITIES = re.compile(r"^capabilities\s*:\s*(#.*)?$")
_DATA_KEY = re.compile(r"^data\s*:")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def with_phi(source: str, capability: str, on: bool) -> str:
    """`source` with `data: phi` set on (or taken off) one capability, every other
    line — comments included — kept as written.

    Edits block-style YAML (`capabilities:` at the top, each capability a block of
    keys). Anything else — flow style, JSON, anchors — is a `SourceEditError`, and
    so is any edit whose result does not parse to exactly the old document with
    that one change: the switch never saves YAML that says something else.
    """
    try:
        before = yaml.safe_load(source) or {}
    except yaml.YAMLError as exc:
        raise SourceEditError(f"the saved YAML does not parse: {exc}") from exc
    caps = before.get("capabilities") if isinstance(before, dict) else None
    if not isinstance(caps, dict) or capability not in caps:
        raise SourceEditError(f"the saved YAML has no capability {capability!r}")
    expected = copy.deepcopy(before)
    cap = dict(expected["capabilities"][capability] or {})
    if on:
        cap["data"] = PHI
    else:
        cap.pop("data", None)
    expected["capabilities"][capability] = cap

    lines = source.split("\n")
    top = next((i for i, ln in enumerate(lines) if _TOP_CAPABILITIES.match(ln)), None)
    if top is None:
        raise SourceEditError("`capabilities:` is not a block at the top of the saved YAML")
    name = re.compile(r"^\s*[\"']?" + re.escape(capability) + r"[\"']?\s*:\s*(#.*)?$")
    head = None
    for i in range(top + 1, len(lines)):
        if _content(lines[i]) and _indent(lines[i]) == 0:
            break
        if _content(lines[i]) and name.match(lines[i]):
            head = i
            break
    if head is None:
        raise SourceEditError(f"{capability!r} is not a block of keys under `capabilities:`")
    end, field = len(lines), None
    for i in range(head + 1, len(lines)):
        if not _content(lines[i]):
            continue
        if _indent(lines[i]) <= _indent(lines[head]):
            end = i
            break
        if field is None:
            field = _indent(lines[i])
    field = field if field is not None else _indent(lines[head]) + 2
    data_lines = [i for i in range(head + 1, end)
                  if _content(lines[i]) and _indent(lines[i]) == field
                  and _DATA_KEY.match(lines[i].lstrip(" "))]
    out = list(lines)
    for i in reversed(data_lines):
        del out[i]
    if on:
        at = data_lines[0] if data_lines else head + 1
        out.insert(at, " " * field + f"data: {PHI}")
    edited = "\n".join(out)
    try:
        after = yaml.safe_load(edited) or {}
    except yaml.YAMLError as exc:
        raise SourceEditError(f"the edit would not parse: {exc}") from exc
    if after != expected:
        raise SourceEditError("the saved YAML is laid out in a way the switch cannot edit by line")
    return edited
