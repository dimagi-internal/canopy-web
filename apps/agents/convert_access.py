"""Translate an agent's published rules into its access list, and prove nothing changed.

An agent's "who may use it" used to be spread over four things: the interface's `full:`
rules, each door's `callers`, the interface's `editors:` setting, and the implicit rule
that a workspace editor is an agent editor. `docs/superpowers/specs/2026-10-30-agent-roles-
and-access-list-design.md` replaces them with one list of `(who, role)` lines. This module
does the translation for ONE agent without touching the database (`plan`), checks the
result against the old rules by asking `access.decide` both ways about everyone who could
plausibly ask (`probes`), and, only if asked, writes it (`apply`).

The translation, line by line:

    full: member[@d]               -> everyone in the agent's workspace [at d]        -> editor
    full: contact[@d][:verified]   -> a known contact [at d] [verified]               -> editor
    a door's callers: the same     -> the same lines                                  -> viewer, or user
                                      (the door's own role: contacts listed => viewer)
    the implicit editor rule       -> everyone in the workspace at least editor      -> editor, manual_only
    (unless `editors: confined`)

A class that is listed on several doors gets the highest of their roles; one in `full:` is
an editor. `unknown` classes (nobody established who) are not translated: they are reported
and the agent is left on its old rules.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model

from apps.contacts.models import Contact
from apps.workspaces import services as wsvc

from . import access, grants, roles
from . import interface as iface_mod
from .models import Agent, AgentGrant

G = AgentGrant


@dataclass
class Line:
    """A line to create, before it is a row."""

    kind: str
    role: str
    subject_workspace: str | None = None
    at_least: str = ""
    domain: str = ""
    verified: bool = False
    manual_only: bool = False
    note: str = ""

    @property
    def key(self) -> str:
        return G.key_of(self.kind, subject_workspace=self.subject_workspace, at_least=self.at_least,
                        domain=self.domain)

    def as_grant(self, agent) -> AgentGrant:
        """An unsaved row, enough for the resolver."""
        g = G(agent=agent, kind=self.kind, role=self.role, subject_workspace_id=self.subject_workspace,
              at_least=self.at_least, domain=self.domain, verified=self.verified,
              manual_only=self.manual_only, note=self.note)
        g.subject_key = self.key
        return g

    def who(self) -> str:
        """The line's `who`, in the access list's own words."""
        return grants.describe(self.as_grant(None))

    def describe(self) -> str:
        return f"{self.who()} -> {self.role}" + (" (manual only)" if self.manual_only else "")


@dataclass
class Difference:
    probe: str
    capability: str | None
    before: tuple
    after: tuple

    def __str__(self) -> str:
        return f"{self.probe} [{self.capability or 'free-form'}]: {self.before} -> {self.after}"


@dataclass
class Conversion:
    agent: Agent
    lines: list[Line] = field(default_factory=list)
    interface: dict = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    differences: list[Difference] = field(default_factory=list)
    probed: int = 0
    already: bool = False       # the agent already runs on its access list: nothing to do

    @property
    def ok(self) -> bool:
        """Safe to apply: every rule was translated and every probe answers as before."""
        return not self.unsupported and not self.differences


def _rank_class(cls: str):
    m = iface_mod._CALLER.match(cls)
    return m["base"], (m["domain"] or ""), bool(m["verified"])


def _line_for(agent, cls: str, role: str) -> Line | None:
    base, domain, verified = _rank_class(cls)
    if base == "member":
        return Line(G.WORKSPACE, role, subject_workspace=agent.workspace_id, domain=domain)
    if base == "contact":
        return Line(G.CONTACT, role, domain=domain, verified=verified)
    return None


def translate(agent) -> tuple[list[Line], dict, list[str]]:
    """(lines, the interface left behind, what could not be translated). No database writes."""
    iface = agent.interface or {}
    wanted: dict[str, str] = {}
    for cls in iface.get("full") or []:
        wanted[cls] = roles.EDITOR
    new_caps = {}
    for name, cap in (iface.get("capabilities") or {}).items():
        door = iface_mod.door_role(cap)
        for cls in cap.get("callers") or []:
            if roles.rank(door) > roles.rank(wanted.get(cls)):
                wanted[cls] = door
        new_caps[name] = {**copy.deepcopy(cap), "callers": [], "role": door}
    lines: dict[str, Line] = {}
    unsupported: list[str] = []
    for cls, role in wanted.items():
        line = _line_for(agent, cls, role)
        if line is None:
            unsupported.append(f"{cls}: nobody-established-who classes have no access-list line")
            continue
        have = lines.get(line.key)
        if have is None or roles.rank(role) > roles.rank(have.role):
            lines[line.key] = line
    if not iface_mod.editors_confined(iface):
        editors = Line(G.WORKSPACE, roles.EDITOR, subject_workspace=agent.workspace_id, at_least="editor",
                       manual_only=True, note="was the implicit rule: a workspace editor is an editor")
        lines.setdefault(editors.key, editors)
    out = {"version": iface_mod.VERSION, "full": [], "capabilities": new_caps, "callers_default": "none"}
    return list(lines.values()), out, unsupported


def _probes(agent, conv_lines: list[Line]):
    """Everyone who could plausibly ask: each member of the agent's workspace, a canopy user
    in none of it, and contacts at every domain a rule names — verified and not — at an
    unrelated domain, and with no address at all."""
    User = get_user_model()
    people = [(f"member {m.user.email} ({m.role})", m.user) for m in wsvc.effective_memberships(agent.workspace_id)]
    people.append(("a canopy user in none of its workspaces", User(pk=-1, username="~outsider", email="outsider@elsewhere.org")))
    domains = {ln.domain for ln in conv_lines if ln.domain}
    for cls in [*((agent.interface or {}).get("full") or []),
                *[c for cap in ((agent.interface or {}).get("capabilities") or {}).values()
                  for c in cap.get("callers") or []]]:
        domains.add(_rank_class(cls)[1])
    domains.discard("")
    out = [(label, who, True) for label, who in people]
    for d in sorted(domains | {"unrelated.example"}):
        for verified in (True, False):
            out.append((f"contact @{d} {'verified' if verified else 'unproven'}",
                        Contact(email=f"someone@{d}", workspace_id=agent.workspace_id), verified))
    out.append(("a contact with no address", Contact(email="", workspace_id=agent.workspace_id), False))
    return out


def _answer(d) -> tuple:
    return (d.access, d.capability, d.manual_only if d.access == access.FULL else None)


def plan(agent) -> Conversion:
    if agent.uses_access_list:
        return Conversion(agent, interface=agent.interface or {}, already=True)
    lines, iface, unsupported = translate(agent)
    conv = Conversion(agent, lines, iface, unsupported)
    if unsupported:
        return conv
    candidate = copy.copy(agent)
    candidate.interface = iface
    candidate.uses_access_list = True
    rows = [ln.as_grant(candidate) for ln in lines]
    door_names = [None, *sorted((agent.interface or {}).get("capabilities") or {})]
    for label, who, verified in _probes(agent, lines):
        for door in door_names:
            before = _answer(access.decide(agent, who, verified=verified, capability=door, lines=[]))
            after = _answer(access.decide(candidate, who, verified=verified, capability=door, lines=rows))
            conv.probed += 1
            if before != after:
                conv.differences.append(Difference(label, door, before, after))
    return conv


def apply(conv: Conversion, *, by=None) -> None:
    """Write the lines, replace the interface, and switch the agent to its access list."""
    if conv.already:
        return
    if not conv.ok:
        raise ValueError(f"{conv.agent.slug} does not convert cleanly: "
                         + "; ".join([*conv.unsupported, *map(str, conv.differences[:5])]))
    agent = conv.agent
    for ln in conv.lines:
        G.objects.update_or_create(
            agent=agent, subject_key=ln.key,
            defaults=dict(kind=ln.kind, role=ln.role, subject_workspace_id=ln.subject_workspace,
                          at_least=ln.at_least, domain=ln.domain, verified=ln.verified,
                          manual_only=ln.manual_only, note=ln.note, granted_by=by))
    agent.interface = conv.interface
    agent.uses_access_list = True
    agent.save(update_fields=["interface", "uses_access_list", "updated_at"])
