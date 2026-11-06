"""Whether a host site of an agent HANDLES SENSITIVE DATA — may serve a host's
sensitive data — an interface setting, `data: sensitive` (spec
2026-10-22-sensitive-data-agents §5).

A host site says `data: sensitive` on the published interface. That is accepted only

* **when it is structurally true** (`structural_refusals`, asked by
  `interface.parse`): the host site reaches its host through canopy's gateway
  (`sites:` is required), and its tools may be only the gateway's site tools
  (`site_tools`, `site_call`, `site_read`), `current_page`, `who_is_asking`, the
  page's own actions (`page_*`) and read-only local tools — no shell, no
  writes, nothing that leaves the box (Drive, email, memory, Slack, the web).
  Anything else is a 422 naming it;
* **from the agent's owner or an admin**, from the browser or a token, like any other
  change to an agent's settings: the `sensitive` field of its host site
  (`answering.set_site`). Every such save is a setting change, audited with its before
  and after (`apps/events/audit.py`).

What it buys, and what reads it:

* **confinement for everyone** — a turn in such a host site, or any turn in a
  sensitive conversation, runs the site's confined profile whoever asked
  (`turn_context.resolve_turn_context`);
* **a sensitive conversation's host data** — the gateway calls a host for a
  sensitive conversation only from such a host site (`host_gateway`);
* **canopy vouches for it to the host** — the gateway's `Canopy-Execution`
  statement carries the setting (`as_claim`), and the host decides whether to
  accept it.
"""
from __future__ import annotations

import fnmatch
import re

SENSITIVE = "sensitive"
DATA_VALUES = frozenset({SENSITIVE})

#: Local tools a host site that handles sensitive data may hold: they read the agent's own checkout.
READ_ONLY_LOCAL = frozenset({"Read", "Grep", "Glob", "Skill", "ToolSearch", "TodoWrite"})
#: canopy's own tools such a host site may hold — and the ONLY canopy tools a
#: sensitive turn's caller token reaches, whatever its interface says now
#: (`harness.caller_tokens.resolve`, `mcp.turn_scope`).
SENSITIVE_CANOPY_TOOLS = frozenset({"current_page", "who_is_asking", "site_tools", "site_call",
                                    "site_read"})
#: The actions the conversation's own page declares (`apps/mcp/page_tools.py`), as a
#: glob over canopy's tool names. Each runs in the person's open tab, as them, and
#: its arguments and answer travel only between the turn and that tab — the
#: conversation's own people (`page_tools.PageActionTool`) — so a sensitive turn may
#: hold them: they put nothing anywhere the conversation's people are not.
PAGE_TOOL_GLOB = "page_*"
#: Those tools as interface patterns name them (any MCP server prefix ending in
#: `canopy-web__`).
_CANOPY_TOOL = re.compile(
    r"^mcp__(?:\*|[A-Za-z0-9_.-]*?)canopy-web__(?:"
    + "|".join(sorted(SENSITIVE_CANOPY_TOOLS)) + r"|page_\*)$")


def sensitive_tool(name: str) -> bool:
    """Whether a sensitive turn may reach canopy's tool `name` at all: one of
    `SENSITIVE_CANOPY_TOOLS`, or one of its page's own actions."""
    return name in SENSITIVE_CANOPY_TOOLS or fnmatch.fnmatchcase(name, PAGE_TOOL_GLOB)


def structural_refusals(name: str, cap: dict) -> list[str]:
    """Why `cap` cannot honestly be declared to handle sensitive data — each named.
    Empty: it can."""
    out = []
    if not cap.get("sites"):
        out.append(f"{name}: a site that handles sensitive data must name `sites:` — a host's data "
                   "reaches it only through canopy's gateway")
    bad = [t for t in cap.get("tools") or []
           if t not in READ_ONLY_LOCAL and not _CANOPY_TOOL.match(t)]
    if bad:
        out.append(f"{name}: a site that handles sensitive data may hold only the gateway's site tools, "
                   f"current_page, who_is_asking, the page's own actions (page_*) and "
                   f"{', '.join(sorted(READ_ONLY_LOCAL))}; "
                   f"refused: {', '.join(bad)}")
    if cap.get("bash"):
        out.append(f"{name}: a site that handles sensitive data may run no shell (`bash:` must be empty)")
    if cap.get("write_paths"):
        out.append(f"{name}: a site that handles sensitive data may write nothing (`write_paths:` must "
                   "be empty)")
    return out


def sensitive_profiles(iface: dict | None) -> list[str]:
    return sorted(n for n, c in ((iface or {}).get("capabilities") or {}).items()
                  if (c or {}).get("data") == SENSITIVE)


def handles_sensitive(agent, name: str | None) -> bool:
    if not name:
        return False
    cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(name)
    return bool(cap) and cap.get("data") == SENSITIVE


def as_claim(agent, name: str | None) -> dict | None:
    """The setting as canopy states it to a host (`Canopy-Execution`, whose `capability` key is wire):
    who last published the interface that holds it, and when. None when the host site `name`
    is not `data: sensitive`."""
    if not handles_sensitive(agent, name):
        return None
    by = agent.interface_published_by
    at = agent.interface_published_at
    return {"data": SENSITIVE, "capability": name,
            "declared_by": getattr(by, "email", "") if by is not None else "",
            "declared_at": at.isoformat() if at else ""}
