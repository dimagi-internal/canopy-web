"""Set up who may use an agent in tests: its interface and its access list.

Who may use an agent is its access list (`AgentGrant` lines: a role, or one named capability)
plus the default lines of its workspace; what the agent offers is its interface (capabilities).
A test that is about something else (a turn's mode, a Slack thread, a widget) still wants
"members may ask" or "workspace editors may drive it, manually", so these three helpers say
that in a line each:

    declare(agent, {"ask": {"tools": ["Read"]}})          # what the agent offers
    allow(agent, role=roles.READ_ONLY)                    # every member may ask (`read` opens `ask`)
    allow(agent, capability="report", contact=True)       # any contact may use `report`
    allow(agent, role=roles.EDITOR, contact=True, domain="dimagi.com", verified=True)
    editors_run(agent)                                    # workspace editors drive it, manual only

Tests that exercise the rule itself build `AgentGrant` rows directly
(`tests/test_access_matrix.py`, `tests/test_access_list_decide.py`).
"""
from __future__ import annotations

from apps.agents import roles
from apps.agents.interface import parse
from apps.agents.models import AgentGrant


def declare(agent, capabilities: dict | None = None, **top_level):
    """Publish `capabilities` as `agent`'s interface (none: an interface that offers nothing).
    Says only what the agent offers, never who may use it. Other top-level keys of the
    interface document (`pages=...`) ride along."""
    agent.interface = parse({**top_level, "capabilities": capabilities}) if capabilities else {"version": 1, "capabilities": {}}
    agent.interface_source = ""
    agent.save(update_fields=["interface", "interface_source"])
    return agent


def editors_run(agent, *, manual_only: bool = True) -> AgentGrant:
    """Everyone in the agent's workspace at least editor may run the whole agent, every turn
    manual (the default line of a workspace)."""
    return AgentGrant.objects.update_or_create(
        agent=agent, subject_key=AgentGrant.key_of(AgentGrant.WORKSPACE, subject_workspace=agent.workspace_id,
                                                   at_least="editor"),
        defaults=dict(kind=AgentGrant.WORKSPACE, subject_workspace_id=agent.workspace_id, at_least="editor",
                      role=roles.EDITOR, manual_only=manual_only, verified=False))[0]


def allow(agent, *, role: str = "", capability: str = "", contact: bool = False,
          domain: str = "", verified: bool = False) -> AgentGrant:
    """One access-list line: `role` (or just the named `capability`) for the agent's workspace
    members, or with `contact=True` for contacts (optionally of one email `domain`, `verified`
    by the message's own proof). A second call for the same subject keeps the higher role."""
    assert bool(role) != bool(capability), "a line names a role or a capability"
    if contact:
        kind, ws, v = AgentGrant.CONTACT, None, verified
    else:
        kind, ws, v = AgentGrant.WORKSPACE, agent.workspace_id, False
    key = AgentGrant.key_of(kind, subject_workspace=ws, at_least="", domain=domain, capability=capability)
    row, _ = AgentGrant.objects.get_or_create(
        agent=agent, subject_key=key,
        defaults=dict(kind=kind, subject_workspace_id=ws, domain=domain, verified=v, role=role,
                      capability=capability))
    if role and roles.rank(role) > roles.rank(row.role):
        row.role = role
        row.save(update_fields=["role"])
    return row
