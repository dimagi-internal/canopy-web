"""What the database says about people and agents — the facts `access.decide` reads.

A leaf under `access`: it reads roles (`wsvc`), access lists (`grants`) and `AgentAdmin`
and decides nothing, so `apps/harness` can ask it without importing the rule.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.workspaces import services as wsvc

from . import grants
from .models import AgentAdmin


@dataclass(frozen=True)
class Standing:
    """What the database says about one person and one agent — all `decide` reads
    about them: their role in the agent's workspace (`wsvc.member_role`, None =
    not a member) and whether they hold an explicit `AgentAdmin` grant (None =
    not read; `Agent.is_admin` reads it if it needs it)."""

    role: str | None
    granted: bool | None = None
    #: Their effective role in EVERY workspace, when the caller read them in bulk
    #: (`wsvc.member_roles_of`) — what an access-list line naming another workspace asks.
    roles: Mapping[str, str] | None = None
    #: The agent's access list, when the caller read it in bulk (`Standings`).
    lines: list | None = None
    #: {workspace: every workspace below it} for the subtree lines of the lists read in bulk.
    descendants: Mapping[str, frozenset[str]] | None = None


def standing_of(user, agent) -> Standing:
    return Standing(wsvc.member_role(user, agent.workspace_id))


class Standings:
    """`standing_of` for many people and many agents, read at once: every role of
    every person (`wsvc.member_roles_of`) and every agent's grants, in a fixed
    number of queries however many pairs are then asked. For a view that judges
    many (person, agent) pairs — the agent topology, the roster — so it asks
    `decide` per pair without a membership read per pair.

    Read per request and dropped with it: not a cache, nothing to invalidate.
    Asking about a person it was not given raises, rather than reading them as
    a non-member."""

    def __init__(self, users, agents) -> None:
        self._roles = wsvc.member_roles_of(users)
        self._lines = grants.lines_for_many(agents)
        subtree = {ln.subject_workspace_id for lines in self._lines.values() for ln in lines
                   if ln.kind == grants.AgentGrant.WORKSPACE and ln.include_below}
        self._below = ({s: frozenset(v) for s, v in wsvc.descendant_map(subtree).items()}
                       if subtree else {})
        self.grants: dict[int, dict[int, AgentAdmin]] = {a.pk: {} for a in agents}
        for g in AgentAdmin.objects.filter(agent__in=self.grants).select_related("granted_by"):
            self.grants[g.agent_id][g.user_id] = g

    def lines_of(self, agent) -> list:
        """The agent's access list (its own lines and the workspace defaults it inherits)."""
        return self._lines[agent.pk]

    def of(self, user, agent) -> Standing:
        mine = self._roles[user.pk]
        return Standing(mine.get(agent.workspace_id), user.pk in self.grants[agent.pk], mine,
                        self._lines[agent.pk], self._below)

    def is_admin(self, user, agent) -> bool:
        """`agent.is_admin(user)`, from what was read."""
        st = self.of(user, agent)
        return agent.is_admin(user, role=st.role, granted=st.granted)
