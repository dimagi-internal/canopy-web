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

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, ancestor_chains

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
    contact: bool = False               # canopy knows them as a contact (not a member): an emailer, a visitor
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
    return list(AgentGrant.objects.filter(scope).select_related("user", "granted_by"))


def lines_for_many(agents) -> dict[int, list[AgentGrant]]:
    """`lines_for` for many agents at once: {agent_id: lines}, in a fixed number of queries
    however many agents — for a view that judges many (person, agent) pairs."""
    agents = list(agents)
    out: dict[int, list[AgentGrant]] = {a.pk: [] for a in agents}
    if not agents:
        return out
    for line in AgentGrant.objects.filter(agent__in=agents).select_related("user"):
        out[line.agent_id].append(line)
    inheriting = [a for a in agents if a.inherit_workspace_defaults and a.workspace_id]
    if not inheriting:
        return out
    homes = {a.workspace_id for a in inheriting}
    chains = ancestor_chains(dict(Workspace.objects.filter(slug__in=homes).values_list("slug", "parent_id")))
    defaults: dict[str, list[AgentGrant]] = {}
    scope = homes | {slug for chain in chains.values() for slug in chain}
    for line in AgentGrant.objects.filter(agent__isnull=True, workspace_id__in=scope).select_related("user"):
        defaults.setdefault(line.workspace_id, []).append(line)
    for a in inheriting:
        for slug in (a.workspace_id, *chains.get(a.workspace_id, [])):
            out[a.pk].extend(defaults.get(slug, []))
    return out


def matches(line: AgentGrant, asker: Asker) -> bool:
    if line.kind == AgentGrant.PERSON:
        return asker.user is not None and getattr(asker.user, "pk", None) == line.user_id
    if line.kind == AgentGrant.WORKSPACE:
        return (perms.role_meets(asker.role_in(line.subject_workspace_id), line.at_least)
                and (not line.domain or asker.domain == line.domain))
    if line.kind == AgentGrant.DOMAIN:
        if line.verified and not asker.verified:
            return False
        return bool(asker.domain) and (not line.domain or asker.domain == line.domain)
    if line.kind == AgentGrant.CONTACT:
        if not asker.contact or (line.verified and not asker.verified):
            return False
        return not line.domain or asker.domain == line.domain
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


# --- editing the list -----------------------------------------------------------------

class LineError(ValueError):
    """A line the list refuses, with a message fit to show next to the form."""


_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")
#: Roles a person can ADD today. `admin` stays `Agent.is_admin` (owner, workspace owner,
#: `AgentAdmin`); folding it into the list is the last step of the spec.
ADDABLE = (roles.EDITOR, roles.USER, roles.VIEWER)
#: Kinds whose members are identified only by an address a message can claim. They can open
#: capabilities (viewer, user); they cannot run the whole agent — that takes a named person or a
#: workspace's people. (Rules published before the list are carried over as they were.)
ADDRESS_ONLY = (AgentGrant.DOMAIN, AgentGrant.CONTACT)


def _person(actor, user_id, email):
    User = get_user_model()
    user = (User.objects.filter(pk=user_id).first() if user_id
            else User.objects.filter(email__iexact=(email or "").strip(), is_active=True).first())
    reach = wsvc.member_scope(actor)
    if user is None or not (wsvc.user_workspace_slugs(user) & reach):
        raise LineError("no such person among the workspaces you can see — they need a canopy account "
                        "in one of them (or use an 'anyone at a domain' line)")
    return user


def check_line(actor, *, kind, role, user_id=None, email=None, workspace="", at_least="", domain="",
               verified=True, scope_is_workspace=False):
    """Validate one line a person is adding; returns the normalised field dict."""
    if role not in ADDABLE:
        raise LineError(f"role must be one of {list(ADDABLE)}; to make someone an admin of the agent, "
                        "add them as an admin")
    if kind in ADDRESS_ONLY and role == roles.EDITOR:
        raise LineError("a line that names people only by their address can give viewer or user, not "
                        "editor: name the person, or everyone in a workspace")
    domain = (domain or "").strip().lower().lstrip("@")
    fields: dict = {"kind": kind, "role": role, "domain": "", "at_least": "", "verified": True}
    if kind == AgentGrant.PERSON:
        fields["user"] = _person(actor, user_id, email)
    elif kind == AgentGrant.WORKSPACE:
        if workspace not in wsvc.member_scope(actor):
            raise LineError(f"you are not in a workspace called {workspace!r}")
        if at_least and not perms.is_role(at_least):
            raise LineError(f"at_least must be a workspace role, not {at_least!r}")
        if domain and not _DOMAIN.match(domain):
            raise LineError(f"{domain!r} is not a domain")
        fields.update(subject_workspace_id=workspace, at_least=at_least, domain=domain, verified=False)
    elif kind in ADDRESS_ONLY:
        if domain and not _DOMAIN.match(domain):
            raise LineError(f"{domain!r} is not a domain")
        if kind == AgentGrant.DOMAIN and not domain:
            raise LineError("'anyone at a domain' needs the domain")
        if not domain and not verified and kind == AgentGrant.DOMAIN:
            raise LineError("an unverified line for any address would admit anyone")
        fields.update(domain=domain, verified=bool(verified))
    else:
        raise LineError(f"unknown kind {kind!r}")
    return fields


def describe(line: AgentGrant) -> str:
    """The line's `who`, in words."""
    d = f" at @{line.domain}" if line.domain else ""
    proof = " (verified)" if line.verified else ""
    if line.kind == AgentGrant.PERSON:
        return getattr(line.user, "email", "") or f"user {line.user_id}"
    if line.kind == AgentGrant.WORKSPACE:
        floor = f", {line.at_least} or above" if line.at_least else ""
        return f"Everyone in {line.subject_workspace_id}{floor}{d}"
    if line.kind == AgentGrant.DOMAIN:
        return f"Anyone{d}{proof}"
    return f"A known visitor or sender{d}{proof}"
