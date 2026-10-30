"""Set up who may use an agent in tests, in the words the tests were written in.

The interface's `full:` rules, each capability's `callers`, `editors:` and the implicit
"a workspace editor is an editor" rule are gone (migration `agents/0112`): who may use an agent
is its access list. A test that is about something else (a turn's mode, a Slack thread, a
widget) still wants "members may ask" or "workspace editors may drive it, manually", so
`publish` takes the old-style document and writes the equivalent capabilities and lines.

    publish(agent, {"capabilities": {"ask": {"callers": ["member", "contact"], "tools": ["Read"]}}})
    publish(agent, HAL, editors=False)       # the old `editors: confined`
    editors_run(agent)                       # just the old default: workspace editors, manual only

The translation is the one the data migration applied; tests that exercise it more finely
live in `tests/test_agent_access_list.py` and `tests/test_access_list_decide.py`.
"""
from __future__ import annotations

import copy
import re

from apps.agents import roles
from apps.agents.interface import parse
from apps.agents.models import AgentGrant

_CALLER = re.compile(r"^(?P<base>member|contact|unknown)"
                     r"(?:@(?P<domain>[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}))?"
                     r"(?::(?P<verified>verified))?$")
_RANK = {roles.VIEWER: 1, roles.USER: 2, roles.EDITOR: 3}


def _capability_role(cap: dict) -> str:
    if cap.get("role") in (roles.VIEWER, roles.USER):
        return cap["role"]
    callers = cap.get("callers") or []
    return roles.VIEWER if any(c.startswith(("contact", "unknown")) for c in callers) else roles.USER


def editors_run(agent, *, manual_only: bool = True) -> AgentGrant:
    """The old default: everyone in the agent's workspace at least editor may run the whole
    agent, every turn manual."""
    return AgentGrant.objects.update_or_create(
        agent=agent, subject_key=AgentGrant.key_of(AgentGrant.WORKSPACE, subject_workspace=agent.workspace_id,
                                                   at_least="editor"),
        defaults=dict(kind=AgentGrant.WORKSPACE, subject_workspace_id=agent.workspace_id, at_least="editor",
                      role=roles.EDITOR, manual_only=manual_only, verified=False))[0]


def let_in(agent, who: str, role: str) -> AgentGrant:
    """One old-style caller class (`member`, `contact@dimagi.com:verified`, ...) as a line."""
    m = _CALLER.match(who)
    assert m and m["base"] != "unknown", f"no line for {who!r}"
    domain, verified = m["domain"] or "", bool(m["verified"])
    if m["base"] == "member":
        kind, ws, v = AgentGrant.WORKSPACE, agent.workspace_id, False
    else:
        kind, ws, v = AgentGrant.CONTACT, None, verified
    key = AgentGrant.key_of(kind, subject_workspace=ws, at_least="", domain=domain)
    row, _ = AgentGrant.objects.get_or_create(
        agent=agent, subject_key=key,
        defaults=dict(kind=kind, subject_workspace_id=ws, domain=domain, verified=v, role=role))
    if _RANK[role] > _RANK[row.role]:
        row.role = role
        row.save(update_fields=["role"])
    return row


def publish(agent, doc: dict | None, *, editors: bool = True):
    """Write the capabilities of an old-style interface `doc` to `agent` and its `full:` rules,
    `callers` and (unless `editors=False`, the old `editors: confined`) the implicit editor rule
    as access-list lines. `doc=None` publishes nothing."""
    doc = copy.deepcopy(doc or {})
    caps = {}
    for name, cap in (doc.get("capabilities") or {}).items():
        role = _capability_role(cap)
        for who in cap.pop("callers", None) or []:
            let_in(agent, who, role)
        cap["role"] = role
        caps[name] = cap
    for who in doc.pop("full", None) or []:
        let_in(agent, who, roles.EDITOR)
    if doc.pop("editors", "full") == "confined":
        editors = False
    doc.pop("callers_default", None)
    agent.interface = parse({**doc, "capabilities": caps}) if caps else {"version": 1, "capabilities": {}}
    agent.interface_source = ""
    agent.save(update_fields=["interface", "interface_source"])
    if editors:
        editors_run(agent)
    return agent
