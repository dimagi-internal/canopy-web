"""A frozen copy of how `Agent.interface` answers today, so the move off it is checked against today.

`docs/superpowers/specs/2026-11-04-agent-settings-replace-the-interface-design.md` § 6. Every
function here reads only the stored `agent.interface` document (and the `interface_published_*`
columns), and repeats what `apps/agents/interface.py`, `interface_default.py`, `access.py`,
`sensitive_data.py`, `site_agents.py`, `execution.py` and `caller_context._profile` do with it.
`tests/test_answering_settings_equivalence.py` holds the real code equal to this.

Do not import the functions it copies, and do not edit it to make that test pass: this file is the
reference. A difference means the real code changed what an agent answers. Facts that are not about
the interface (who the owner is, what an access-list line grants, which runners take the agent) are
still read from the real code: they are not changing.
"""
from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase

from apps.agents import access, grants, roles
from apps.harness import runner_requirements as rr
from apps.harness import sensitivity
from apps.host_grants import execution, site_agents
from apps.workspaces.links import scoped_url

ASK, READ = "ask", "read"
READ_MODE_NAMES = (ASK, READ)
READ_PROFILE_VERSION = 3
FULL = ""
SENSITIVE = "sensitive"
GATEWAY_TOOLS = ("mcp__*canopy-web__site_tools", "mcp__*canopy-web__site_call",
                 "mcp__*canopy-web__site_read")
PAGE_TOOLS = ("mcp__*canopy-web__page_*",)
SENSITIVE_REFUSAL = ("this conversation holds a host's sensitive data, so it runs only in a "
                     "capability its author declared handles sensitive data (`data: sensitive`), "
                     "and {slug} has none for it{you}")


def _doc(agent) -> dict:
    return getattr(agent, "interface", None) or {}


def _caps(agent) -> dict:
    return _doc(agent).get("capabilities") or {}


def is_read_mode(name) -> bool:
    return name in READ_MODE_NAMES


def read_mode_override(iface) -> str | None:
    caps = (iface or {}).get("capabilities") or {}
    return next((n for n in READ_MODE_NAMES if n in caps), None)


def published_capability(iface, name):
    caps = (iface or {}).get("capabilities") or {}
    if name in caps:
        return name, caps[name]
    if is_read_mode(name) and (own := read_mode_override(iface)):
        return own, caps[own]
    return None, None


def opens(permissions, name: str) -> bool:
    if is_read_mode(name):
        return roles.ROLES.holds(permissions, roles.READ) or any(
            roles.ROLES.holds(permissions, f"{roles.USE}:{n}") for n in READ_MODE_NAMES)
    return roles.ROLES.holds(permissions, f"{roles.USE}:{name}")


# --- the profile a turn runs in ---------------------------------------------------------------

def default_read_profile(agent) -> dict:
    slug = agent.slug
    scratch = [f"{{cwd}}/.{slug}-{suffix}/*" for suffix in (READ, ASK)]
    names = set(agent.skills.values_list("name", flat=True))
    bash: list[str] = []
    if (getattr(agent, "email", "") or "").strip():
        bash.append("canopy email read --repo . {thread_id}")
        bash += [f"canopy email review-receipt --repo . --body-file {d}" for d in scratch]
        bash += [f"bin/{slug}-email --reply-all --thread-id {{thread_id}} --subject-file {d} --body-file {d}"
                 for d in scratch]
    return {
        "description": f"Ask {slug} a question. It answers from its own repo and docs; "
                       "it cannot change anything but draft its reply.",
        "entry": f"/{slug}:answer-caller --thread {{thread_id}}" if "answer-caller" in names else None,
        "tools": ["Read", "Write", "ToolSearch", "mcp__*canopy-web__who_is_asking"],
        "bash": bash,
        "read_paths": ["{cwd}/**"],
        "write_paths": scratch,
        "pages": [],
        "sites": [],
        "ceiling": [],
    }


def profile(agent, capability: str) -> dict | None:
    if capability == FULL:
        return None
    _published, cap = published_capability(getattr(agent, "interface", None), capability)
    default_version = None
    if cap is None and is_read_mode(capability):
        cap, default_version = default_read_profile(agent), READ_PROFILE_VERSION
    cap = cap or {"description": "", "entry": None, "tools": [], "bash": [], "read_paths": [],
                  "write_paths": [], "pages": [], "sites": [], "ceiling": []}
    out = {"name": capability, **{k: cap.get(k) for k in
                                  ("description", "entry", "tools", "bash", "read_paths", "write_paths")}}
    if default_version is not None:
        out["default_profile_version"] = default_version
    if cap.get("data"):
        out["data"] = cap["data"]
    if cap.get("sites"):
        out["tools"] = list(dict.fromkeys([*(out.get("tools") or []), *GATEWAY_TOOLS, *PAGE_TOOLS]))
    return out


def envelope_profile(agent, capability: str, turn=None):
    if agent is None or not capability:
        return None
    got = profile(agent, capability)
    if "default_profile_version" in got and turn is not None and sensitivity.is_sensitive(turn):
        return {"name": capability, "description": "", "entry": None, "tools": [], "bash": [],
                "read_paths": [], "write_paths": []}
    return got


def reachable(permissions, iface) -> list[str]:
    caps = (iface or {}).get("capabilities") or {}
    names = [n for n in caps if opens(permissions, n)]
    if read_mode_override(iface) is None and opens(permissions, READ):
        names.append(READ)
    return sorted(names)


# --- the sites an agent serves ----------------------------------------------------------------

def page_sites(agent) -> set[str]:
    return {site for cap in _caps(agent).values() for site in (cap.get("sites") or [])}


def _caps_for_site(agent, site: str) -> list[dict]:
    caps = _caps(agent)
    return [caps[n] for n in sorted(caps) if site in (caps[n].get("sites") or [])]


def _conversation_site(turn) -> str:
    session = getattr(turn, "chat_session", None)
    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    site = meta.get("embed_app")
    return site.strip() if isinstance(site, str) else ""


def _declared_resource(turn) -> str:
    session = getattr(turn, "chat_session", None)
    state = getattr(session, "page_state", None) or {} if session is not None else {}
    resource = state.get("resource")
    return resource if isinstance(resource, str) else ""


def page_site_of(turn, agent) -> str:
    site = _conversation_site(turn)
    if agent is None or not site:
        return ""
    return site if site in page_sites(agent) else ""


def site_ceiling(agent, site: str) -> list[str]:
    caps = _caps_for_site(agent, site)
    if not caps or any(not cap.get("ceiling") for cap in caps):
        return []
    return list(dict.fromkeys(g for cap in caps for g in cap.get("ceiling") or []))


def own_login_of(agent, site: str) -> list[str]:
    return sorted({n for cap in _caps_for_site(agent, site)
                   for n in (cap.get("own_login") or {}).get(site) or []})


def page_turn(turn, agent) -> dict | None:
    site = page_site_of(turn, agent)
    if not site:
        return None
    return {"site": site, "withheld": own_login_of(agent, site)}


# --- sensitive data, as hosts are told it -----------------------------------------------------

def sensitive_capabilities(iface) -> list[str]:
    return sorted(n for n, c in ((iface or {}).get("capabilities") or {}).items()
                  if (c or {}).get("data") == SENSITIVE)


def handles_sensitive(agent, capability) -> bool:
    if not capability:
        return False
    cap = _caps(agent).get(capability)
    return bool(cap) and cap.get("data") == SENSITIVE


def as_claim(agent, capability) -> dict | None:
    if not handles_sensitive(agent, capability):
        return None
    by, at = agent.interface_published_by, agent.interface_published_at
    return {"data": SENSITIVE, "capability": capability,
            "declared_by": getattr(by, "email", "") if by is not None else "",
            "declared_at": at.isoformat() if at else ""}


def facts_for(turn, agent, session, requirements) -> dict:
    return {
        "agent": agent.slug,
        "capability": turn.capability or "",
        "declaration": as_claim(agent, turn.capability),
        "runner": execution.runner_facts(turn.claimed_by),
        "runner_posture": execution.posture_facts(turn.claimed_by),
        "requirements": sorted(requirements),
        "turn": str(turn.pk),
        "session": str(session.pk) if session is not None else "",
    }


def _person(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or user.email or ""


def site_capabilities(agent, site: str) -> list[dict]:
    out = []
    caps = _caps(agent)
    for name in sorted(caps):
        cap = caps[name] or {}
        if site not in (cap.get("sites") or []):
            continue
        claim = as_claim(agent, name)
        out.append({
            "name": name,
            "description": cap.get("description") or "",
            "handles_sensitive": claim is not None,
            "sensitive_set_by": _person(agent.interface_published_by) if claim else "",
            "sensitive_set_at": (agent.interface_published_at.isoformat()
                                 if claim and agent.interface_published_at else ""),
        })
    return out


def agent_entry(app, agent) -> dict:
    capabilities = site_capabilities(agent, app.name)
    takers, ready_runners = site_agents._runners(agent)
    available = sorted(set().union(*(rr.in_force(r) for r in takers))) if takers else []
    flags = sorted(site_agents.contract.RUNNER_FLAGS)
    ready_with = {f: any(f in rr.in_force(r) for r in ready_runners) for f in flags}
    settings_url = scoped_url(agent.workspace_id, f"/agents/{agent.slug}/settings")
    reach, running = f"{settings_url}#reach", f"{settings_url}#running"
    found: list[tuple[str, str]] = []
    if not capabilities:
        found.append((f"{agent.name} is not connected to {app.name} — connect it under Channels on its settings: canopy › {reach}", reach))
    if not takers:
        found.append((f"no runner takes {agent.name}'s turns: canopy › {running}", running))
    elif not ready_runners:
        names = ", ".join(sorted(r.name for r in takers))
        found.append((f"no runner that takes {agent.name}'s turns is ready now ({names}): "
                      f"canopy › {running}", running))
    if any(c["handles_sensitive"] for c in capabilities) and not ready_with.get(rr.SENSITIVE_DATA):
        found.append((f"no ready runner with {rr.SENSITIVE_DATA} for {agent.name}: "
                      f"canopy › {running}", running))
    return {
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description or "",
        "workspace": agent.workspace_id,
        "owner": _person(agent.owner),
        "url": scoped_url(agent.workspace_id, f"/agents/{agent.slug}"),
        "capabilities": capabilities,
        "runners": {"flags_available": available, "ready_with": ready_with},
        "ready": bool(capabilities) and bool(ready_runners),
        "problems": [text for text, _url in found],
        "problem_links": [{"text": text, "url": url} for text, url in found],
    }


# --- the decision: every part of `access.decide` that reads the interface ---------------------

def _page_capability(turn, iface, permissions) -> str | None:
    resource = _declared_resource(turn)
    if not resource:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        patterns = cap.get("pages") or []
        if not patterns or not opens(permissions, name):
            continue
        if any(fnmatchcase(resource, pattern) for pattern in patterns):
            return name
    return None


def _site_capability(turn, iface, permissions) -> str | None:
    site = _conversation_site(turn)
    if not site:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        if site in (cap.get("sites") or []) and opens(permissions, name):
            return name
    return None


def _capability_for(iface, permissions, capability, turn):
    name = capability
    if name is None and turn is not None:
        name = _page_capability(turn, iface, permissions) or _site_capability(turn, iface, permissions)
    name = name or ASK
    published, cap = published_capability(iface, name)
    if published is None and is_read_mode(name):
        published, cap = READ, {}
    return (published, cap) if cap is not None and opens(permissions, published) else (None, None)


def _from_access_list(agent, d, who, *, verified, capability, turn, standing, lines):
    if d.access == access.FULL:
        return d
    asker = access._asker_of(who, turn, verified, standing)
    got = grants.resolve(agent, asker, lines if lines is not None else getattr(standing, "lines", None))
    if not got.permissions:
        return d
    iface = _doc(agent)
    if roles.holds(got.permissions, roles.WRITE):
        line = got.line_for(roles.WRITE)
        return access.Decision(relationship=d.relationship, access=access.FULL, capability=None,
                               may_request_auto=False, may_pin_runner=False, manual_only=got.manual_only,
                               basis=got.basis, agent_role=got.role, source=f"list:{line.subject_key}",
                               via=grants.describe(line), permissions=got.permissions)
    name, _cap = _capability_for(iface, got.permissions, capability, turn)
    if name is None:
        return d
    line = got.line_opening(name)
    return access.Decision(relationship=d.relationship, access=access.CONFINED, capability=name,
                           may_request_auto=False, may_pin_runner=False, manual_only=False,
                           basis=f"capability:{name}", agent_role=got.role,
                           source=f"list:{line.subject_key}", via=grants.describe(line),
                           permissions=got.permissions)


def _sensitive_capability(iface, capability, turn) -> str | None:
    caps = iface.get("capabilities") or {}
    if capability:
        published, cap = published_capability(iface, capability)
        return published if (cap or {}).get("data") == SENSITIVE else None
    if turn is None:
        return None
    site, resource = _conversation_site(turn), _declared_resource(turn)
    for name, cap in caps.items():
        if cap.get("data") != SENSITIVE:
            continue
        if site and site in (cap.get("sites") or []):
            return name
        if resource and any(fnmatchcase(resource, p) for p in cap.get("pages") or []):
            return name
    return None


def _sensitive_rule(agent, d, *, capability, turn):
    name = _sensitive_capability(_doc(agent), capability, turn)
    sensitive = turn is not None and sensitivity.is_sensitive(turn)
    if name is None and not sensitive:
        return d
    if name is not None and d.access == access.FULL:
        return replace(d, access=access.CONFINED, capability=name, basis=f"sensitive:{name}")
    if d.access == access.NONE or not sensitive:
        return d
    if name is not None and d.access == access.CONFINED and d.capability == name:
        return d
    you = "" if name is None else f" that is open to you ('{name}' does not list you)"
    return replace(d, access=access.NONE, capability=None, may_request_auto=False, may_pin_runner=False,
                   manual_only=False, basis="refused", agent_role=None, source="",
                   permissions=frozenset(), reason=SENSITIVE_REFUSAL.format(slug=agent.slug, you=you))


def decide(agent, who=None, *, verified: bool, origin: str = "", capability=None, turn=None,
           standing=None, lines=None):
    d = access._stamp(access._decide(agent, who, verified=verified, origin=origin, capability=capability,
                                     turn=turn, st=standing))
    d = _from_access_list(agent, d, who, verified=verified, capability=capability, turn=turn,
                          standing=standing, lines=lines)
    return _sensitive_rule(agent, d, capability=capability, turn=turn)
