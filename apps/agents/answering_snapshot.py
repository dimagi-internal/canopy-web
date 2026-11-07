"""How every agent answers, as one read-only document that hashes the same until the data changes.

    manage.py answering_snapshot [--json]

Taken before and after each step of replacing the interface's YAML with plain settings
(`docs/superpowers/specs/2026-11-04-agent-settings-replace-the-interface-design.md`), so that a step
that changes an answer shows up as a diff. It holds, per agent: `profile()` per stamp, the the rule's
matrix, the access list and roster, `facts_for` when the agent is sensitive, and its A2A cards; per
registered site, what the site-agents API would sign; per workspace tree, the topology edges; and
`shape`, the stored facts that design counts on, checked rather than assumed.

It only reads. It signs, mints and emits nothing, and takes the people in its matrix from memory,
never from a row it writes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from apps.admission.models import AdmissionRule
from apps.canopy_sessions.models import Session
from apps.contacts.models import Contact
from apps.harness import initiator as initiators
from apps.harness import runner_requirements as rr
from apps.harness.models import Turn
from apps.harness.routing import serves_refusal
from apps.host_grants import execution, site_agents
from apps.tokens.models import AppCredential
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import access, agent_card, profile_settings, roles, sensitive_data, topology, turn_context
from . import published_profiles as profiles_mod
from .models import Agent, AgentGrant, AgentRole
from .setup import runner_takers
from .standings import Membership

#: A stamp nothing was ever published under: it must keep resolving to the deny-all profile.
RETIRED_STAMP = "retired_probe"
#: A conversation on a site no agent serves: it must keep falling back to read mode.
OTHER_SITE = "site_no_agent_serves"
#: The ids a probe turn carries, fixed so a statement's `turn` and `session` do not change per run.
PROBE_TURN = uuid.UUID(int=1)
PROBE_SESSION = uuid.UUID(int=2)
_PROBE_PK = -1
_PROBE_DOMAIN = "snapshot.invalid"

#: What a token or key looks like, for the last check before anything is printed. Canopy's own
#: prefixes, a signed JWT, GitHub, AWS, Anthropic, sealed values and PEM blocks.
_SECRET_SHAPES = re.compile(
    r"\b(?:cpat|cact|cdt|ccon|cct|crt|chk|hcpat|hcprt)_[A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bsk-ant-[A-Za-z0-9_-]{8,}"
    r"|\bzks?1:[A-Za-z0-9+/_=-]{8,}|-----BEGIN ")

#: The statements a read may send. A `WITH` is left out on purpose: it can write.
_READS = ("select", "(select", "savepoint", "release savepoint", "rollback to savepoint")


class SnapshotError(Exception):
    """The snapshot would have written, or would have printed something shaped like a secret."""


def _refuse_writes(execute, sql, params, many, context):
    head = sql.lstrip().lower()
    if not head.startswith(_READS) or " for update" in head:
        raise SnapshotError(f"refused a statement that is not a read: {sql.strip()[:60]}")
    return execute(sql, params, many, context)


def take() -> dict:
    """The snapshot as plain JSON values. Raises SnapshotError rather than write or leak."""
    # Three locks, because this runs against production: PostgreSQL refuses a write in a read-only
    # transaction, the wrapper refuses it on any backend and names it, and the block always rolls
    # back. Rolling back also restores the transaction mode when a test wraps this in its own.
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
        with connection.execute_wrapper(_refuse_writes):
            doc = _build()
        transaction.set_rollback(True)
    plain = json.loads(json.dumps(doc, default=_encode))
    _refuse_secrets(plain, "")
    return plain


def as_json(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)


def digest(doc: dict) -> str:
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _encode(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"the snapshot does not know how to print a {type(value).__name__}")


def _refuse_secrets(value, path: str) -> None:
    # Names the place, never the value: this error reaches a terminal and a log.
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_secrets(key, f"{path}/<key>")
            _refuse_secrets(item, f"{path}/{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _refuse_secrets(item, f"{path}/{i}")
    elif isinstance(value, str) and _SECRET_SHAPES.search(value):
        raise SnapshotError(f"refused to print a value shaped like a secret at {path}")


def _build() -> dict:
    agents = list(Agent.objects.select_related("workspace", "owner", "user", "interface_published_by")
                  .order_by("slug"))
    members = {slug: wsvc.effective_memberships(slug) for slug in {a.workspace_id for a in agents}}
    people = ({a.owner for a in agents if a.owner_id} | {a.user for a in agents if a.user_id}
              | {m.user for ms in members.values() for m in ms})
    standings = access.Standings(people, agents)
    live_sites = list(AppCredential.objects.filter(revoked_at__isnull=True).order_by("workspace_id", "name"))
    sites_of_workspace: dict[str, set[str]] = {}
    for app in live_sites:
        sites_of_workspace.setdefault(app.workspace_id, set()).add(app.name)
    return {
        "agents": {a.slug: _agent(a, members[a.workspace_id], standings,
                                  sites_of_workspace.get(a.workspace_id, set())) for a in agents},
        "sites": {f"{app.workspace_id}/{app.name}": _site(app) for app in live_sites},
        "topology": {ws.slug: _edges(ws) for ws in Workspace.objects.filter(parent__isnull=True).order_by("slug")},
        "shape": _shape(agents),
    }


# --- what the plain settings count on ---------------------------------------------------------

def _shape(agents) -> dict:
    # Where else an entry's name is stored: a name in any of these can never be renamed.
    published = {a.pk: set((a.interface or {}).get("capabilities") or {}) for a in agents}
    slugs = {a.pk: a.slug for a in agents}
    named_lines = list(AgentGrant.objects.exclude(capability="").values_list("agent_id", "capability"))
    dangling = sorted(f"{slugs[agent_id]}: {name}" for agent_id, name in named_lines
                      if agent_id and not profiles_mod.is_read_mode(name) and name not in published[agent_id])
    problems = {a.slug: profile_settings.document_problems((a.interface or {}).get("capabilities") or {})
                for a in agents}
    role_names = {p for row in AgentRole.objects.values_list("permissions", flat=True) for p in row or []
                  if p.startswith(f"{roles.USE}:") and p != f"{roles.USE}:*"}
    return {
        "agent_problems": {slug: found for slug, found in problems.items() if found},
        "lines_naming_nothing_published": dangling,
        "workspace_lines_naming": sorted({name for agent_id, name in named_lines if not agent_id}),
        "roles_naming": sorted(role_names),
        "admission_rules_naming": sorted(set(AdmissionRule.objects.exclude(capability="")
                                             .values_list("capability", flat=True))),
    }


# --- one agent --------------------------------------------------------------------------------

def _agent(agent, members, standings, workspace_sites: set[str]) -> dict:
    caps = sorted((agent.interface or {}).get("capabilities") or {})
    stamps = [profiles_mod.FULL_STAMP, *profiles_mod.READ_MODE_NAMES, *caps, RETIRED_STAMP]
    served = profiles_mod.page_sites(agent)
    roster = access.roster(agent, members, standings)
    out = {
        "workspace": agent.workspace_id,
        "profile": {stamp: profiles_mod.profile(agent, stamp) for stamp in stamps},
        "sites_served": {site: {"ceiling": profiles_mod.site_ceiling(agent, site),
                                "own_login": profiles_mod.own_login_of(agent, site)} for site in sorted(served)},
        "lines": _lines(standings.lines_of(agent)),
        "decide": _matrix(agent, caps, sorted(served | workspace_sites), members, roster, standings),
        "roster": [{k: row[k] for k in ("email", "agent_role", "access", "profiles", "role", "source",
                                        "via", "manual_only", "may_request_auto")} for row in roster],
        "a2a": _cards(agent, members, roster),
    }
    if sensitive_data.sensitive_profiles(agent.interface):
        out["facts_for"] = _statements(agent, stamps)
    return out


def _lines(lines) -> list[dict]:
    # `note` is left out: it is free text a person typed, and it answers nothing.
    rows = [{"kind": ln.kind, "subject": ln.subject_key, "role": ln.role, "capability": ln.capability,
             "permissions": sorted(ln.permissions), "manual_only": ln.manual_only, "verified": ln.verified,
             "scope": f"agent:{ln.agent_id}" if ln.agent_id else f"workspace:{ln.workspace_id}"} for ln in lines]
    return sorted(rows, key=lambda r: (r["scope"], r["subject"]))


def probe_turn(*, user=None, contact=None, site: str = "", sensitive: bool = False, stamp: str = "",
               runner=None) -> Turn:
    """A turn in memory, never saved: what the rule and `facts_for` would be handed for a conversation
    on `site`, sensitive or not, started by `user` or `contact`."""
    metadata = {}
    if site:
        metadata["embed_app"] = site
    if sensitive:
        metadata[rr.METADATA_KEY] = [rr.SENSITIVE_DATA]
    kind = initiators.USER if user is not None else initiators.CONTACT
    return Turn(pk=PROBE_TURN, chat_session=Session(pk=PROBE_SESSION, metadata=metadata), capability=stamp,
                claimed_by=runner, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, initiator_kind=kind,
                initiator_user=user, initiator_contact=contact,
                initiator_assurance=initiators.SESSION if user is not None else "")


def _probe_person(role: str) -> tuple:
    # In memory: a person nothing in the database knows, whom one line in memory gives `role`. So the
    # matrix asks what each posture gets on EVERY agent, whatever its real lines happen to be, without
    # adding a line. Real lines are judged too, in `roster`.
    user = get_user_model()(pk=_PROBE_PK, email=f"{role}@{_PROBE_DOMAIN}", username=f"probe-{role}")
    row = AgentRole(pk=_PROBE_PK, key=role, permissions=sorted(roles.permissions_of(role)))
    line = AgentGrant(kind=AgentGrant.PERSON, user=user, role=role, role_def=row, verified=False,
                      subject_key=f"probe:{role}")
    return user, Membership(None, False, {}, [line], {}), [line]


def _probe_contact(site: str) -> tuple:
    contact = Contact(email=f"contact@{_PROBE_DOMAIN}")
    line = AgentGrant(kind=AgentGrant.CONTACT, domain=_PROBE_DOMAIN, capability=site, verified=False,
                      subject_key=f"probe:use:{site}")
    return contact, None, [line]


def _personas(agent, caps, members, roster, standings) -> list[tuple]:
    """`(label, who, standing, lines)`: the owner and an admin, if it has them (they are real people:
    only the database can make someone an admin), then each posture a line can give."""
    out = []
    if agent.owner_id:
        out.append(("owner", agent.owner, standings.of(agent.owner, agent), standings.lines_of(agent)))
    users = {m.user.pk: m.user for m in members}
    admin = next((users[r["user_id"]] for r in roster
                  if r["agent_role"] == access.ADMIN and r["user_id"] != agent.owner_id), None)
    if admin is not None:
        out.append(("admin", admin, standings.of(admin, agent), standings.lines_of(agent)))
    for role in (roles.EDITOR, roles.READ_ONLY):
        out.append((role, *_probe_person(role)))
    for name in caps:
        if not profiles_mod.is_read_mode(name):
            out.append((f"contact_use_{name}", *_probe_contact(name)))
    return out


def _contexts(sites: list[str]) -> list[tuple]:
    """`(label, has a turn, site, sensitive)`: no conversation at all, one with no site, and one on
    each site, each of those ordinary and sensitive."""
    out = [("no_turn", False, "", False), ("chat", True, "", False), ("sensitive_chat", True, "", True)]
    for site in [*sites, OTHER_SITE]:
        out += [(f"site:{site}", True, site, False), (f"sensitive_site:{site}", True, site, True)]
    return out


def _matrix(agent, caps, sites, members, roster, standings) -> dict:
    asks = [None, *profiles_mod.READ_MODE_NAMES, *caps, RETIRED_STAMP]
    out: dict = {}
    for label, who, standing, lines in _personas(agent, caps, members, roster, standings):
        by_context = out.setdefault(label, {})
        for context, has_turn, site, sensitive in _contexts(sites):
            turn = None
            if has_turn:
                is_user = isinstance(who, get_user_model())
                turn = probe_turn(user=who if is_user else None, contact=None if is_user else who,
                                  site=site, sensitive=sensitive)
            by_context[context] = {
                ask or "(free-form)": answer_of(turn_context.for_asker(
                    agent, who, verified=isinstance(who, get_user_model()), requested=ask, turn=turn,
                    standing=standing, lines=lines))
                for ask in asks}
    return out


def answer_of(ctx) -> dict:
    """A Turn Context as this document has always printed an answer, so that its hash keeps meaning the
    same thing: `access` says `none` for a refusal, and a refusal's reason is `reason`: its whole message, why and
    how to fix it, so that a change of either shows here."""
    return {
        "relationship": ctx.standing.relationship,
        "access": "none" if ctx.refused else ctx.access,
        "capability": ctx.stamped or None,
        "may_request_auto": ctx.review.may_request_auto,
        "may_pin_runner": ctx.review.may_pin_runner,
        "manual_only": ctx.review.manual_only,
        "basis": ctx.basis,
        "reason": ctx.refusal.message if ctx.refused else "",
        "agent_role": ctx.standing.agent_role,
        "source": ctx.standing.source,
        "via": ctx.standing.via,
        "permissions": sorted(ctx.standing.permissions),
        "stamp": ctx.stamped,
    }


def _statements(agent, stamps) -> dict:
    # Every stamp on no runner and on each box that would take the turn: the facts, unsigned.
    takers = sorted((r for r in runner_takers(agent)[0] if not serves_refusal(r, agent.slug)),
                    key=lambda r: (r.name, str(r.pk)))
    out = {}
    for runner in [None, *takers]:
        out[runner.name if runner is not None else "(no runner)"] = {
            stamp: execution.facts_for(probe_turn(stamp=stamp, runner=runner), agent, None,
                                       {rr.SENSITIVE_DATA})
            for stamp in stamps}
    return out


def _cards(agent, members, roster) -> dict:
    # An extended card depends only on what the viewer may use, so one per distinct answer: a person
    # is named by neither the key nor the card, and joining a workspace does not add a card.
    users = {m.user.pk: m.user for m in members}
    extended = {}
    for row in sorted(roster, key=lambda r: (r["access"], r["profiles"], r["user_id"])):
        key = (row["access"], tuple(row["profiles"]))
        if key not in extended:
            card = agent_card.extended_card(agent, users[row["user_id"]])
            extended[key] = {"access": row["access"], "profiles": row["profiles"],
                             "card": agent_card.dump(card) if card is not None else None}
    public = agent_card.public_card(agent)
    return {"public": agent_card.dump(public) if public is not None else None,
            "extended": list(extended.values())}


# --- one site, one tree -----------------------------------------------------------------------

def _site(app) -> dict:
    content = site_agents.site_agents_content(app)
    return {**content, "agents": [_without_liveness(entry) for entry in content["agents"]]}


def _without_liveness(entry: dict) -> dict:
    # What depends on a box's last heartbeat changes with no change of data, so it would make two
    # runs differ for nothing: `ready`, `ready_with`, and the problems that point at `#running`. What
    # the interface decides is kept, including the problem that points at the agent's settings.
    keep = [p for p in entry["problem_links"] if not p["url"].endswith("#running")]
    out = {k: v for k, v in entry.items() if k != "ready"}
    return {**out, "runners": {"flags_available": entry["runners"]["flags_available"]},
            "problems": [p["text"] for p in keep], "problem_links": keep}


def _edges(root) -> list[dict]:
    # `can_grant` and `can_revoke` are about whoever is looking, and nobody is.
    edges = topology.build(root, None)["edges"]
    rows = [{k: v for k, v in e.items() if k not in ("can_grant", "can_revoke")} for e in edges]
    return sorted(rows, key=lambda e: (e["source"], e["target"]))
