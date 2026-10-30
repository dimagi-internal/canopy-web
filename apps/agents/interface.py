"""An agent's DECLARED CAPABILITIES: what it offers people who are not its admins.

Who may use which capability is NOT here: it is the agent's access list
(`apps/agents/grants.py`, `docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`).
This says only what each capability IS — its tools, paths and sites — and the lowest role
that may use it (`role: viewer | user`). THE rule that decides who gets which is
`apps/agents/access.decide`; every term is defined in `docs/architecture/access.md`.

An agent's owner and admins reach its full working session; so does anyone the access list
makes an editor. Everyone else — a user, a viewer — reaches only a capability. It is LIVE STATE
held by canopy-web, never a file in the agent's repo, and canopy enforces it.

**Allowlist by construction.** Restricting a fully-powered agent per caller —
instructions, a policy file, a hook with a denylist — leaks. So a caller turn
runs in its capability's profile, where everything not listed is denied:

    capabilities:
      ask:                                  # the free-form capability every channel uses
        description: Ask ACE about your programme.
        role: viewer                        # the lowest role that may use it (viewer | user)
        entry: /ace:ask --thread {thread_id}  # the session's prompt (optional)
        tools: [Read, Grep, "mcp__canopy-web__who_is_asking"]
        bash: ["canopy email read --repo . {thread_id}"]
        read_paths: ["{cwd}/**"]
        write_paths: ["{cwd}/.ace-ask/*"]    # where Write/Edit may land (none → no writes)

**A page can pick the capability.** A capability may name the resources it serves,
and a conversation held on a page that declared one of them runs there instead
of in `ask`:

    capabilities:
      marketplace:
        role: viewer
        pages: ["labs-marketplace://*"]   # what the PAGE declared it is showing
        tools: [Skill, "mcp__*canopy-web__current_page",
                "mcp__*connect_labs__marketplace_*"]

This exists because an embedded page already publishes a contract — `setPageState`
says what it is showing and which tool resolves those rows — and until now nothing
could answer it. Every free-form channel collapsed to `ask`, so an agent embedded
in a page had one capability shared with its email, and the only way to give the page
what it needed was to give an email stranger the same thing. That is the wrong
trade in both directions: the page's conversation is under-equipped and the
mailbox is over-exposed.

It cannot widen anything on the host's word: the page supplies only the
resource it is showing; every tool in the profile was chosen by the agent's owner. A host
that lies about its resource reaches exactly the tools that owner already decided
to expose to that page — and it had to be an allowlisted app, framing from a
registered origin, to open a conversation at all.

**`pages:` is superseded by `sites:` + `ceiling:`** (host grant contract v1,
2026-09-26) and kept only so interfaces already published keep working. A
per-page tool list had to be written by hand, page by page, and every tool on it
ran as the AGENT. A site capability instead names a registered Connected site:

    capabilities:
      connect:
        role: viewer
        sites: [connect-labs]               # a Connected site, by name
        # ceiling: ["mcp__*connect_labs__marketplace_*"]   # OPTIONAL narrowing

A conversation held on that site (its server-owned `embed_app`) selects the
capability, and its confined profile gains canopy's gateway tools (`site_tools`,
`site_call`, `site_read`) and the page's own actions (`page_*`) automatically.
Through the gateway the agent calls the host's tools AS THE VISITOR, with a grant
the host issued for the page they are on — so what one turn may call is what the
HOST grants: its scopes for that page and its ACL for that person
(`apps/host_grants/host_gateway.py`). canopy holds no list of the host's
tools. `ceiling:` is an optional narrowing on the owner's word (keep an agent off
part of a site); absent, canopy adds none. It can never widen what the host
granted. The page's `backing_tool` tells the agent where to read the rows on
screen; it is not a filter (until 2026-09-29 it was).

**Default deny.** Every agent HAS an interface; until its owner changes it, it is the default
(`default_interface()`: no capability). An agent that has published nothing is reachable by its
owner, admins and anyone its access list makes an editor, and by nobody else. A capability
with no `pages` is never selected by a page. An agent with no `ask` capability cannot be asked
by a viewer even if the list names them: a line can only open a capability that exists.

**Removed 2026-10-30:** `full:`, each capability's `callers:`, `callers_default:` and
`editors:`. A published interface that still carries them is refused with a message pointing
at the access list; the data migration `agents/0112` moved every agent's rules onto its list.
"""
from __future__ import annotations

import re

from apps.agents import roles
from apps.agents.interface_default import VERSION, default_interface  # noqa: F401  (re-exported)
from apps.canopy_sessions.models import SessionParticipant
from apps.harness import initiator as who
from apps.harness import invocations

#: The fallback capability for a free-form channel (email, chat, Slack, a widget on a
#: page that declared nothing). Named capabilities are for callers that ask for
#: something specific, or for a page whose declared resource matches one's
#: `pages` — see `_page_capability`.
ASK = "ask"

#: canopy's gateway to a Connected site's own tools, added to every capability
#: that names `sites:` (apps/mcp/tools/site.py).
GATEWAY_TOOLS = ("mcp__*canopy-web__site_tools", "mcp__*canopy-web__site_call",
                 "mcp__*canopy-web__site_read")

#: The actions the conversation's own page declares (`apps/mcp/page_tools.py`), also
#: added to every capability that names `sites:`: a site conversation is held on a
#: page, and what that page offers is part of the capability. Each runs in the visitor's
#: own tab, as them; a confined turn reaches only its own conversation's page.
PAGE_TOOLS = ("mcp__*canopy-web__page_*",)

#: `Turn.capability` for a turn in the agent's FULL profile. Empty so that every
#: turn predating this reads as "full", which is what it was.
FULL = ""

#: The lowest role that may use a capability (`role:` on it). Who HAS a role is the
#: access list's business (`apps/agents/grants.py`); a capability only says how much it takes.
CAPABILITY_ROLES = (roles.VIEWER, roles.USER)


def capability_role(cap: dict) -> str:
    """The lowest role that may use `cap`: its `role:`, `user` when it says nothing."""
    explicit = cap.get("role")
    return explicit if explicit in CAPABILITY_ROLES else roles.USER


def role_opens(role: str | None, cap: dict) -> bool:
    """May someone holding `role` use `cap`?"""
    return roles.at_least(role, capability_role(cap))

#: What a capability's `input` fields may be. Declared and shown, but no capability
#: passes typed inputs today: the MCP capability tools that did were removed
#: (agents are used through their plugin, not canopy's MCP).
INPUT_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
#: Names an `input` field may not take (the message envelope's own fields).
_RESERVED_INPUTS = frozenset({"message", "conversation_id", "wait_seconds"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MAX_ITEMS = 100
_MAX_PATTERN = 300


class InterfaceError(ValueError):
    """A refused interface. `path` says which field it is about (`full`,
    `ask.tools`, `ask`), so a form can show the message next to that field; it is
    read off the message's leading `where:`/`where ` when the raiser gave none."""

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message)
        self.path = path if path is not None else _path_of(message)


_PATH = re.compile(r"^(?P<path>[a-z][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)(?::|\s)")


def _path_of(message: str) -> str:
    m = _PATH.match(message)
    return m["path"] if m else ""


def _strings(value, field: str, cap: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise InterfaceError(f"{cap}.{field} must be a list of at most {_MAX_ITEMS} strings")
    out = []
    for v in value:
        if not isinstance(v, str) or not v.strip() or len(v) > _MAX_PATTERN:
            raise InterfaceError(f"{cap}.{field} entries must be non-empty strings "
                                 f"of at most {_MAX_PATTERN} characters")
        out.append(v.strip())
    return out


#: Keys the access list replaced; refused with the pointer below, never silently ignored.
_RETIRED = {"full", "editors", "callers"}
_RETIRED_HINT = ("{where}: `{key}:` is gone — who may use an agent is its access list now "
                 "(Settings → Access, or /api/agents/<slug>/access-list). A capability only says what "
                 "it is and the lowest `role:` (viewer or user) that may use it")


def parse(doc) -> dict:
    """Validate and normalise a published interface. Raises InterfaceError.

    Strict on purpose: an unknown key is refused rather than ignored, because
    a misspelt `tools:` silently dropped would publish a profile that allows
    less than its author thinks.
    """
    if not isinstance(doc, dict):
        raise InterfaceError("the interface must be a mapping")
    for key in sorted(_RETIRED & set(doc)):
        raise InterfaceError(_RETIRED_HINT.format(where="interface", key=key), key)
    extra = set(doc) - {"capabilities", "callers_default", "version"}
    if extra:
        raise InterfaceError(f"unknown top-level key(s): {sorted(extra)}")
    if doc.get("callers_default", "none") != "none":
        raise InterfaceError("callers_default is gone: nobody is let in by default")
    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict) or len(caps) > 50:
        raise InterfaceError("capabilities must be a mapping of at most 50 entries")
    out: dict = {}
    for name, cap in caps.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise InterfaceError(f"capability name {name!r} must match {_NAME.pattern}")
        if not isinstance(cap, dict):
            raise InterfaceError(f"{name} must be a mapping")
        if "callers" in cap:
            raise InterfaceError(_RETIRED_HINT.format(where=name, key="callers"), f"{name}.callers")
        bad = set(cap) - {"description", "entry", "tools", "bash", "read_paths",
                          "write_paths", "input", "pages", "sites", "ceiling", "own_login",
                          "data", "role"}
        if bad:
            raise InterfaceError(f"{name}: unknown key(s) {sorted(bad)}")
        entry = cap.get("entry")
        if entry is not None and (not isinstance(entry, str) or "\n" in entry
                                  or len(entry) > _MAX_PATTERN
                                  or invocations.parse(entry) is None):
            raise InterfaceError(f"{name}.entry must be one plugin command: /<plugin>:<skill> [args]")
        inputs = cap.get("input") or {}
        if not isinstance(inputs, dict) or len(inputs) > 20:
            raise InterfaceError(f"{name}.input must be a mapping of at most 20 fields")
        for field, typ in inputs.items():
            if not isinstance(field, str) or not _NAME.match(field) or field in _RESERVED_INPUTS:
                raise InterfaceError(f"{name}.input: {field!r} must match {_NAME.pattern} "
                                     f"and not be one of {sorted(_RESERVED_INPUTS)}")
            if typ not in INPUT_TYPES:
                raise InterfaceError(f"{name}.input.{field}: type must be one of {sorted(INPUT_TYPES)}")
        role = cap.get("role", roles.USER)
        if role not in CAPABILITY_ROLES:
            raise InterfaceError(f"{name}.role must be one of {list(CAPABILITY_ROLES)}", f"{name}.role")
        out[name] = {
            "input": dict(inputs),
            "description": str(cap.get("description") or "")[:500],
            "role": role,
            "entry": entry,
            "tools": _strings(cap.get("tools"), "tools", name),
            "bash": _strings(cap.get("bash"), "bash", name),
            "read_paths": _strings(cap.get("read_paths"), "read_paths", name),
            # Separate from read_paths on purpose (2026-09-26): with one list, a
            # caller who may Write anywhere they may Read could overwrite the very
            # script their `bash` allowlist lets them run (`bin/ace-email`), and
            # then run it. The guard refuses Write/Edit outside these; none → no
            # writes at all.
            "write_paths": _strings(cap.get("write_paths"), "write_paths", name),
            "pages": _pages(cap.get("pages"), name),
            **_sites(cap, name),
        }
        if cap.get("data") is not None:
            out[name]["data"] = _data(cap["data"], name, out[name])
    return {"version": VERSION, "capabilities": out}


_SITE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _data(value, name: str, cap: dict) -> str:
    """`data:` — what a capability may serve. `sensitive` is an author's
    declaration that it handles sensitive data, refused when its profile shows it
    cannot (spec 2026-10-22-sensitive-data-agents §5; `apps/agents/sensitive_data.py`)."""
    from . import sensitive_data

    if value not in sensitive_data.DATA_VALUES:
        raise InterfaceError(f"{name}.data must be one of {sorted(sensitive_data.DATA_VALUES)}")
    refusals = sensitive_data.structural_refusals(name, cap)
    if refusals:
        raise InterfaceError("; ".join(refusals))
    return value


def _sites(cap: dict, name: str) -> dict:
    """`sites:` (Connected site names) and `ceiling:` (tool globs), validated.

    A ceiling with no site is refused: it would read as a grant and apply to
    nothing. A site with no ceiling defers to the host: what the visitor's grant
    reaches is the host's decision (`host_gateway.tool_allowed`).
    """
    sites = _strings(cap.get("sites"), "sites", name)
    for site in sites:
        if not _SITE.match(site):
            raise InterfaceError(f"{name}.sites: {site!r} is not a Connected site name — "
                                 "letters, digits, hyphens and underscores")
    ceiling = _strings(cap.get("ceiling"), "ceiling", name)
    if ceiling and not sites:
        raise InterfaceError(f"{name}.ceiling needs `sites:` — a ceiling bounds the tools of "
                             "the sites it names")
    out = {"sites": sites, "ceiling": ceiling}
    own = _own_login(cap.get("own_login"), name, sites)
    if own:
        # Written only when present, so every interface published before the key
        # existed parses to exactly what it did.
        out["own_login"] = own
    return out


_CREDENTIAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")


def _own_login(value, name: str, sites: list[str]) -> dict[str, list[str]]:
    """`own_login:` — per site, the names of the agent's OWN credentials that are its
    login at that site (e.g. `{connect-labs: [LABS_MCP_TOKEN]}`). A turn in a
    conversation held on that site's page never holds them (`page_site_of`): there
    the agent reaches the site only through the person's act-as-you permission."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > _MAX_ITEMS:
        raise InterfaceError(f"{name}.own_login must be a mapping of site name to credential names")
    out: dict[str, list[str]] = {}
    for site, names in value.items():
        if site not in sites:
            raise InterfaceError(f"{name}.own_login: {site!r} is not one of this capability's "
                                 "`sites:` — it names the agent's login at a site it serves")
        listed = _strings(names, f"own_login.{site}", name)
        for n in listed:
            if not _CREDENTIAL_NAME.match(n):
                raise InterfaceError(f"{name}.own_login.{site}: {n!r} is not a credential name "
                                     "(an environment variable name, e.g. LABS_MCP_TOKEN)")
        if listed:
            out[site] = sorted(set(listed))
    return out


def _pages(value, cap: str) -> list[str]:
    """Resource patterns a capability serves, e.g. `labs-marketplace://*`.

    An embedded page already declares what it is showing (`setPageState`), and
    that declaration is server-stored and server-versioned. This is how an agent
    ANSWERS it: the page says which room it is in, the owner says what is in that
    room. A `*` is the only wildcard, matched with `fnmatch`, and a bare `*` is
    refused — "any page at all" is what leaving `pages` off already means, and
    spelling it as a pattern reads like a narrowing while being the opposite.
    """
    out = _strings(value, "pages", cap)
    for pattern in out:
        if pattern == "*":
            raise InterfaceError(
                f"{cap}.pages: '*' matches every page, which is what omitting "
                "`pages` does — name the resources this capability is for")
        if "://" not in pattern:
            raise InterfaceError(
                f"{cap}.pages: {pattern!r} is not a resource pattern — it needs a "
                "scheme, as the page's own `resource` does (e.g. stock://*)")
    return out


def _declared_resource(turn) -> str:
    """What the page attached to this turn's conversation says it is showing."""
    session = getattr(turn, "chat_session", None)
    state = getattr(session, "page_state", None) or {} if session is not None else {}
    resource = state.get("resource")
    return resource if isinstance(resource, str) else ""


def _page_capability(turn, iface: dict, opens) -> str | None:
    """The capability whose `pages` match what this turn's page declared.

    Checked before falling back to `ask`, so a conversation held on a page the
    agent has declared a capability for gets THAT capability, and every other channel is
    unaffected. Names are tried in order for determinism; a capability the
    caller's class is not listed for is skipped rather than refusing outright,
    because `ask` may still admit them.

    This cannot widen anything on the host's word: the page supplies only the
    resource it is showing, and every tool in the profile was chosen by the
    agent's owner. A host that lies about its resource reaches the tools that
    owner already decided to expose to that page.
    """
    from fnmatch import fnmatchcase

    resource = _declared_resource(turn)
    if not resource:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        patterns = cap.get("pages") or []
        if not patterns or not opens(cap):
            continue
        if any(fnmatchcase(resource, pattern) for pattern in patterns):
            return name
    return None


def _conversation_site(turn) -> str:
    """The Connected site this turn's conversation is held on — the session's
    server-owned `embed_app`, stamped from the token that created it, never from
    anything the page or the caller sent."""
    session = getattr(turn, "chat_session", None)
    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    site = meta.get("embed_app")
    return site.strip() if isinstance(site, str) else ""


def _site_capability(turn, iface: dict, opens) -> str | None:
    """The capability whose `sites` include the site this conversation is on.

    Tried after `_page_capability` (a page-specific capability is the narrower one)
    and before falling back to `ask`. A capability the caller's class is not
    listed for is skipped, exactly as for pages.
    """
    site = _conversation_site(turn)
    if not site:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        if site in (cap.get("sites") or []) and opens(cap):
            return name
    return None


# --- the sites an agent serves on pages ------------------------------------------
#
# An agent is set up for a Connected site when some capability of its published
# interface lists the site in `sites:`. A conversation held on that site's page
# (`embed_app`) then reaches the site ONLY as the person who sent, under their
# act-as-you permission for the page (their host grant, `host_grants.lookup`) —
# whoever they are, the agent's owner and admins included — and never with the
# agent's own login there (`own_login:`), which such a turn does not hold.


def page_sites(agent) -> set[str]:
    """The Connected sites `agent` serves on pages: every site some capability of its
    interface lists in `sites:`."""
    caps = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).values()
    return {site for cap in caps for site in (cap.get("sites") or [])}


def _caps_for_site(agent, site: str) -> list[dict]:
    caps = ((getattr(agent, "interface", None) or {}).get("capabilities") or {})
    return [caps[n] for n in sorted(caps) if site in (caps[n].get("sites") or [])]


def page_site_of(turn, agent=None) -> str:
    """The site this turn's conversation is held on, when its agent serves that site on
    pages — else "". The one question "is this a page turn for site X" asks, whatever
    the turn's profile (confined or full)."""
    agent = agent if agent is not None else getattr(turn, "acting_agent", None)
    site = _conversation_site(turn)
    if agent is None or not site:
        return ""
    return site if site in page_sites(agent) else ""


def site_ceiling(agent, site: str) -> list[str]:
    """The owner's narrowing of `site`'s tools for a FULL-profile turn on its page: the
    union of the ceilings of the capabilities that list it, or none (the host's grant
    decides) when any of them sets none — a full turn gets the widest capability the owner
    opened on that site, never more."""
    caps = _caps_for_site(agent, site)
    if not caps or any(not cap.get("ceiling") for cap in caps):
        return []
    return list(dict.fromkeys(g for cap in caps for g in cap.get("ceiling") or []))


def own_login_of(agent, site: str) -> list[str]:
    """The names of the agent's own credentials that are its login at `site`
    (`own_login:` of every capability that lists the site). Withheld from every turn
    held on that site's page."""
    return sorted({n for cap in _caps_for_site(agent, site)
                   for n in (cap.get("own_login") or {}).get(site) or []})


def page_turn(turn, agent=None) -> dict | None:
    """What a turn held on a site's page is told, and what its runner withholds:
    `{"site", "withheld"}` — the site, and the agent's own credential names for it
    (`own_login_of`) — or None for any other turn. The claim carries it to the
    runner (`ClaimedTurnOut.page_site`) and the caller envelope to the agent."""
    agent = agent if agent is not None else getattr(turn, "acting_agent", None)
    site = page_site_of(turn, agent)
    if not site:
        return None
    return {"site": site, "withheld": own_login_of(agent, site)}


def published(iface: dict) -> bool:
    """Whether the agent has declared a capability. An interface with none says nothing
    and is treated as none."""
    return bool(iface.get("capabilities"))


#: THE default interface (`default_interface`, from `interface_default.py`), which every agent
#: has until its owner changes it: no capability. Only its owner, its admins and the editors
#: its access list names reach it.
def is_default(iface: dict | None) -> bool:
    """Whether this interface is the default (or empty, which reads the same)."""
    return not published(iface or {})


def validate(doc) -> list[dict]:
    """Every problem with a draft, as `[{path, message}]` — [] when `parse` accepts it.

    `parse` stops at the first fault; a form wants one per field, so the top level
    and each capability are parsed on their own. The server's parser stays the
    only judge: this never decides what is valid, it only asks `parse` more than once.
    """
    errors: list[dict] = []

    def attempt(piece, path: str = ""):
        try:
            parse(piece)
        except InterfaceError as exc:
            errors.append({"path": exc.path or path, "message": str(exc)})

    if not isinstance(doc, dict):
        attempt(doc)
        return errors
    attempt({k: v for k, v in doc.items() if k != "capabilities"})
    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict):
        attempt({"capabilities": caps}, "capabilities")
        return errors
    if len(caps) > 50:
        attempt({"capabilities": caps}, "capabilities")
    for name, cap in caps.items():
        attempt({"capabilities": {name: cap}}, str(name))
    return errors


def session_writer(turn, agent) -> str | None:
    """The asker's role in the session this turn continues, when someone who
    holds the agent's whole profile STARTED that session and gave the asker an
    owner/editor participant row in it — else None.

    A person let into the owner's conversation is part of it: their turn
    continues the SAME session, in its profile. Confining them to a capability
    instead keys their turn to a separate `cx-` session on the runner, so a
    colleague replying in the owner's Slack thread landed in a fresh session the
    owner never saw (Jonathan, 2026-10-01: "if the user has access to my
    session, it should come back in the session I already started").

    Both halves are load-bearing. The creator must be the agent's owner or an
    admin: otherwise any member could start their own chat with the agent and
    be "the owner of a session", which would undo the interface for everyone.
    And the grant must be an explicit row: `role_for` also makes every tenant
    member an editor of a runner-discovered session, a convenience for the web
    UI rather than anyone being let into this conversation.
    """
    if agent is None or turn.initiator_kind != who.USER or not getattr(turn, "chat_session_id", None):
        return None
    user, session = turn.initiator_user, turn.chat_session
    creator = session.created_by
    if user is None or creator is None:
        return None
    if creator.pk != agent.owner_id and not agent.is_admin(creator):
        return None

    role = (SessionParticipant.objects.filter(session=session, user=user)
            .values_list("role", flat=True).first())
    return role if role in (SessionParticipant.OWNER, SessionParticipant.EDITOR) else None


def capability_for(turn, agent, requested: str | None = None) -> str | None:
    """Which profile this turn runs in: FULL, a capability name, or None (refused).

    A thin face over THE rule, `apps.agents.access.decide` — see
    docs/architecture/access.md. In short: FULL for its owner, admins, canopy's own turns,
    a writer in an admin's own session and anyone the access list makes an editor; a user or
    a viewer gets the capability they asked for — `requested`, e.g. a tool called over MCP —
    or the page's/site's capability, or `ask`, if their role may use it; otherwise they are
    refused. An agent that has published no capability lets in only those who run it whole.
    """
    from . import access  # cycle: agents.access -> agents.interface  # noqa: PLC0415

    return access.decide_for_turn(turn, agent, requested).stamp


def granted_by(turn, agent) -> str:
    """WHY this turn has the access it has, for the envelope: `owner`, `admin`, `system`,
    `session:<role>`, `grant:<line>`, `capability:<name>` or `refused`."""
    return grant_facts(turn, agent)["granted_by"]


def grant_facts(turn, agent) -> dict:
    """The envelope's account of the asker's standing, from ONE decision: `granted_by`
    (the older why-word above), `role` (where they stand on the agent's role ladder, None
    when refused), `source` (what put them there: owner | admin | system |
    capability:<name> | session | list:<access-list line>)."""
    from . import access  # cycle: agents.access -> agents.interface  # noqa: PLC0415

    d = access.decide_for_turn(turn, agent, turn.capability or None)
    if d.access == access.NONE and turn.capability:
        d = access.decide_for_turn(turn, agent)   # the stamped capability no longer admits them: as before
    # The capability the turn was STAMPED with at enqueue (a requested one may differ from
    # the free-form one `decide` falls back to).
    why = f"capability:{turn.capability or d.capability}" if d.access == access.CONFINED else d.basis
    return {"granted_by": why, "role": d.agent_role, "source": d.source}


def offered_to(user, agent) -> list[str]:
    """The capabilities `user` may use on `agent` directly — their extended A2A card.

    Everything for anyone whose turns run in the whole agent anyway (owner, admins, editors);
    for anyone else, the capabilities their role on the agent's access list may use. Nothing
    for someone the list does not name.
    """
    caps = ((getattr(agent, "interface", None) or {}).get("capabilities") or {})
    if not caps or not getattr(user, "is_authenticated", False):
        return []
    from . import access  # cycle: agents.access -> agents.interface  # noqa: PLC0415

    d = access.decide(agent, user, verified=True)
    if d.access == access.FULL:
        return sorted(caps)
    if d.access == access.CONFINED:
        return sorted(n for n, c in caps.items() if role_opens(d.agent_role, c))
    return []


def profile(agent, capability: str) -> dict | None:
    """The capability's profile as the runner and the guard see it, or None for FULL.

    A capability that has since been unpublished yields an EMPTY profile —
    deny everything — never the full one: a turn that was restricted when it
    was queued must not become unrestricted because the file changed.
    """
    if capability == FULL:
        return None
    cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(capability)
    cap = cap or {"description": "", "entry": None,
                  "tools": [], "bash": [], "read_paths": [], "write_paths": [], "input": {},
                  "pages": [], "sites": [], "ceiling": []}
    out = {"name": capability, **{k: cap.get(k) for k in
                                  ("description", "entry", "tools", "bash", "read_paths",
                                   "write_paths", "input")}}
    if cap.get("data"):
        out["data"] = cap["data"]
    if cap.get("sites"):
        # A site capability reaches its host ONLY through canopy's gateway, so
        # the gateway is part of the profile by construction — an owner cannot
        # publish a site capability and forget the one tool that opens it. The host's
        # own MCP server is not added: a visitor turn does not call it directly.
        # The page's own actions likewise: they are the page's, not the host's
        # (`site_call` refuses them by name and points here).
        out["tools"] = list(dict.fromkeys([*(out.get("tools") or []), *GATEWAY_TOOLS,
                                           *PAGE_TOOLS]))
    return out
