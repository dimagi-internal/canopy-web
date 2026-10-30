"""The access list: who has which role on an agent.

One list per agent says `(who, role)` — see
`docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md` for the
vocabulary. `who` is a person, everyone in a workspace (optionally "at least editor"
there), or anyone whose address is at a domain. A person's role is the strongest of the
lines that name them; this module is the only thing that reads the lines.

The agent's own lines are joined by the lines of its workspace and the workspaces above
it (a workspace-wide or Dimagi-wide default, set once), unless the agent opts out
(`Agent.inherit_workspace_defaults`). The agent's owner and its admins (`Agent.is_admin`)
are not lines here yet: `access.decide` asks them first.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from django.db.models import Q

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import roles
from .models import AgentGrant


@dataclass(frozen=True)
class Asker:
    """What the resolver needs to know about whoever is asking. Not a database row: the
    caller supplies it, so the rule is the same for a signed-in person, an email sender
    and a Slack member."""

    user: object | None = None          # a canopy User, or None for someone only known by address
    email: str = ""                     # the address they arrive from, any case
    verified: bool = False              # THIS message proves `email` (a signed-in request always does)
    #: Their effective workspace roles by slug, when the caller read them in bulk
    #: (`wsvc.member_roles_of`). None = read them here, one workspace at a time.
    workspace_roles: Mapping[str, str] | None = field(default=None, compare=False)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].strip().lower() if "@" in self.email else ""

    def role_in(self, slug: str) -> str | None:
        if self.workspace_roles is not None:
            return self.workspace_roles.get(slug)
        return wsvc.member_role(self.user, slug) if getattr(self.user, "is_authenticated", False) else None


@dataclass(frozen=True)
class Resolution:
    """The answer for one asker on one agent."""

    role: str | None                     # the strongest matching line's role; None = no line names them
    manual_only: bool = False            # every line at that role demands approval (one without it frees them)
    lines: tuple[AgentGrant, ...] = ()   # every line that matched, strongest first

    @property
    def basis(self) -> str:
        return f"grant:{self.lines[0].subject_key}" if self.lines else "none"


def lines_for(agent) -> list[AgentGrant]:
    """The agent's own lines plus the default lines of its workspace and the ones above."""
    scope = Q(agent=agent)
    if agent.inherit_workspace_defaults and agent.workspace_id:
        chain = [agent.workspace_id, *agent.workspace.ancestor_slugs()]
        scope |= Q(agent__isnull=True, workspace_id__in=chain)
    return list(AgentGrant.objects.filter(scope).select_related("user"))


def matches(line: AgentGrant, asker: Asker) -> bool:
    if line.kind == AgentGrant.PERSON:
        return asker.user is not None and getattr(asker.user, "pk", None) == line.user_id
    if line.kind == AgentGrant.WORKSPACE:
        return perms.role_meets(asker.role_in(line.subject_workspace_id), line.at_least)
    if line.kind == AgentGrant.DOMAIN:
        if line.verified and not asker.verified:
            return False
        return bool(asker.domain) and (not line.domain or asker.domain == line.domain)
    return False


def resolve(agent, asker: Asker, lines: list[AgentGrant] | None = None) -> Resolution:
    """The strongest role the access list gives `asker` on `agent`.

    `lines` is `lines_for(agent)` when the caller already read it (a roster resolving
    many people)."""
    found = [ln for ln in (lines_for(agent) if lines is None else lines) if matches(ln, asker)]
    if not found:
        return Resolution(None)
    found.sort(key=lambda ln: -roles.rank(ln.role))
    top = [ln for ln in found if ln.role == found[0].role]
    return Resolution(found[0].role, manual_only=all(ln.manual_only for ln in top), lines=tuple(found))
