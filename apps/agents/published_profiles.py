"""An agent's PUBLISHED PROFILES: what it offers people who are not its admins.

A profile is one entry of the agent's published document (stored under its `capabilities` key):
read mode, or a host site. Other things canopy names "capability" are different concepts:
a workspace permission (`apps/workspaces/permissions.py`, `docs/architecture/roles.md`); a leased
cloud machine or pool slot (`apps/capabilities`, `docs/architecture/capabilities.md`);
`Runner.capabilities`, the JSON a box reports about itself (`apps/harness/models.py`,
`docs/architecture/runners.md`); and `Storyboard.capability`, the level a share link grants
(`apps/storyboards/models.py`).

Who may use which profile is NOT here: it is the agent's access list
(`apps/agents/grants.py`).
This says only what each profile IS — its tools, paths and sites — and never who may use
it (a profile no longer carries a `role:`). THE rule that decides who gets which is
`apps/agents/turn_context.resolve_turn_context`; every term is defined in `docs/architecture/access.md`.

An agent's owner and admins reach its full working session; so does anyone the access list
gives `write`. Everyone else — a `read_only` line, a line naming one host site — reaches only
a confined profile. It is LIVE STATE
held by canopy-web, never a file in the agent's repo, and canopy enforces it.

**Allowlist by construction.** Restricting a fully-powered agent per caller —
instructions, a policy file, a hook with a denylist — leaks. So a caller turn
runs in its profile, where everything not listed is denied (read mode's override is stored
under `READ_MODE_STAMP`):

    capabilities:
      ask:                                  # read mode, the free-form profile every channel uses
        description: Ask ACE about your programme.
        entry: /ace:answer-caller --thread {thread_id}  # the session's prompt (optional)
        tools: [Read, Grep, "mcp__canopy-web__who_is_asking"]
        bash: ["canopy email read --repo . {thread_id}"]
        read_paths: ["{cwd}/**"]
        write_paths: ["{cwd}/.ace-read/*"]   # where Write/Edit may land (none → no writes)

**A host site's profile** names a registered host site:

    capabilities:
      connect:
        sites: [connect-labs]               # a host site, by name
        # ceiling: ["mcp__*connect_labs__marketplace_*"]   # OPTIONAL site tool limit ("Site tools it may use")

One entry per site, and one site per entry (`answering`, the one writer, enforces both).

**An app entry** names ONE workspace app instead of a site (`apps: [<key>]`); its tools are canopy's
own and fixed (`apps/agents/app_entries.py`), and `ceiling` there can only narrow them.
A conversation held on that site (its server-owned `embed_app`) selects the
profile, and its confined profile gains canopy's gateway tools (`site_tools`,
`site_call`, `site_read`) and the page's own actions (`page_*`) automatically.
Through the gateway the agent calls the host's tools AS THE VISITOR, with a grant
the host issued for the page they are on — so what one turn may call is what the
HOST grants: its scopes for that page and its ACL for that person
(`apps/host_grants/host_gateway.py`). canopy holds no list of the host's
tools. `ceiling:` (the settings form's "Site tools it may use"; the name is stored and sent as `ceiling`) is an optional limit on the owner's word (keep an agent off
part of a site); absent, canopy adds none. It can never widen what the host
granted. The page's `backing_tool` tells the agent where to read the rows on
screen; it is not a filter.

**Read mode is on for every agent.** Every agent HAS an interface; until its owner changes it, it is
the default (`default_interface()`: no profile). Someone whose lines give `read` and not
`write` is answered in READ MODE, a confined session that exists from the start: the agent's own
published read mode when it has one (an override that replaces the default whole), else canopy's
`default_read_profile`. `read` and `READ_MODE_STAMP` are the same thing (`is_read_mode`). Any OTHER
profile is a host site and must be published: a line can only open a site that exists, and an
unknown or retired name yields the empty deny-all profile.

A profile carries no `role:`, `full:`, `callers:`, `callers_default:` or `editors:`: who may use
one is an access-list line (a role, or one that names the host site). A published interface that
still carries them is refused with a message pointing at the access list.
"""
from __future__ import annotations

import logging
import re

from apps.agents import roles
from apps.agents.read_mode import (  # noqa: F401  (re-exported)
    READ_MODE_NAME,
    READ_MODE_NAMES,
    READ_MODE_STAMP,
    READ_PROFILE_VERSION,
    default_interface,
    default_read_profile,
    is_read_mode,
)
from apps.harness import invocations

logger = logging.getLogger(__name__)

#: canopy's gateway to a host site's own tools, added to every profile
#: that names `sites:` (apps/mcp/tools/site.py).
GATEWAY_TOOLS = ("mcp__*canopy-web__site_tools", "mcp__*canopy-web__site_call",
                 "mcp__*canopy-web__site_read")

#: The actions the conversation's own page declares (`apps/mcp/page_tools.py`), also
#: added to every profile that names `sites:`: a site conversation is held on a
#: page, and what that page offers is part of the profile. Each runs in the visitor's
#: own tab, as them; a confined turn reaches only its own conversation's page.
PAGE_TOOLS = ("mcp__*canopy-web__page_*",)

#: The profile every turn of a one-session review runs in, whoever asks: canopy's, built here, on
#: every agent, never publishable (`parse`) and never widened by a published one. It reads the
#: asked session and replies, because that transcript is text someone may have aimed at it.
SESSION_REVIEW = "session_review"
#: What it holds. No Bash, no Write, no other canopy tool. `read_asked_session` takes no
#: session id; canopy reads the one the conversation's live review request names.
SESSION_REVIEW_TOOLS = ("Read", "ToolSearch", "mcp__*canopy-web__who_is_asking",
                        "mcp__*canopy-web__read_asked_session")

#: `Turn.capability` for a turn in the agent's FULL profile. Empty so that every
#: turn predating this reads as "full", which is what it was.
FULL_STAMP = ""

def read_mode_override(iface: dict | None) -> str | None:
    """The name of the agent's own published read-mode profile (`READ_MODE_STAMP`, else `read`), or
    None when it published none. While read mode resolves only through a published profile this
    is the one a `read`-named lookup finds."""
    caps = (iface or {}).get("capabilities") or {}
    return next((n for n in READ_MODE_NAMES if n in caps), None)


def stamp_words(stamp: str) -> str:
    """A confined stamp in the words a person reads: `read mode`, `a one-session review`, or `the
    host site 'x'`. The stamp itself is a stored name, never shown bare."""
    if is_read_mode(stamp):
        return "read mode"
    if stamp == SESSION_REVIEW:
        return "a one-session review"
    return f"the host site '{stamp}'"


def published_profile(iface: dict | None, name: str) -> tuple[str, dict] | tuple[None, None]:
    """`(published name, profile)` for `name`, with `read` and `READ_MODE_STAMP` standing for each other
    (`is_read_mode`), or `(None, None)`. The published name is what a turn is stamped with."""
    caps = (iface or {}).get("capabilities") or {}
    if name in caps:
        return name, caps[name]
    if is_read_mode(name) and (own := read_mode_override(iface)):
        return own, caps[own]
    return None, None


def opens(permissions, name: str) -> bool:
    """May someone holding `permissions` (the union of their access-list lines) use the profile
    `name`? `write` opens every profile, `read` opens read mode, a `use:<name>` line opens `<name>`.
    A profile does not say what it takes: that is the access list's business
    (`apps/agents/grants.py`)."""
    return roles.can_use(permissions, name)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MAX_ITEMS = 100
_MAX_PATTERN = 300


class InterfaceError(ValueError):
    """A refused interface. `path` says which field it is about (`full`,
    `<name>.tools`, `<name>`), so a form can show the message next to that field; it is
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
                 "(Settings → Roles → Access, or /api/agents/<slug>/access-list). A profile only "
                 "says what it may do; to let someone use just this host site, add an access-list "
                 "line that names it")


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
    # Fields nothing reads any more. Published interfaces still carry them (`version: 1` and a
    # profile's `input: {}` were written by every save), and a republish must not 422, so they
    # are accepted, ignored and not written back.
    ignored = sorted(k for k in ("callers_default", "version") if k in doc)
    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict) or len(caps) > 50:
        raise InterfaceError("`capabilities` must be a mapping of at most 50 entries")
    out: dict = {}
    for name, cap in caps.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise InterfaceError(f"profile name {name!r} must match {_NAME.pattern}")
        if name == SESSION_REVIEW:
            raise InterfaceError(f"profile name {name!r} is canopy's own and cannot be published")
        if not isinstance(cap, dict):
            raise InterfaceError(f"{name} must be a mapping")
        for key in ("callers", "role"):
            if key in cap:
                raise InterfaceError(_RETIRED_HINT.format(where=name, key=key), f"{name}.{key}")
        if "pages" in cap:
            raise InterfaceError(f"{name}.pages is gone: a profile is selected by the host site its "
                                 "conversation is held on (`sites:`), never by a page's resource", f"{name}.pages")
        if "input" in cap:
            ignored.append(f"{name}.input")
        if "apps" in cap:
            out[name] = _app_entry(cap, name)
            continue
        bad = set(cap) - {"description", "entry", "tools", "bash", "read_paths",
                          "write_paths", "input", "sites", "ceiling", "own_login", "data"}
        if bad:
            raise InterfaceError(f"{name}: unknown key(s) {sorted(bad)}")
        entry = cap.get("entry")
        if entry is not None and (not isinstance(entry, str) or "\n" in entry
                                  or len(entry) > _MAX_PATTERN
                                  or invocations.parse(entry) is None):
            raise InterfaceError(f"{name}.entry must be one plugin command: /<plugin>:<skill> [args]")
        out[name] = {
            "description": str(cap.get("description") or "")[:500],
            "entry": entry,
            "tools": _strings(cap.get("tools"), "tools", name),
            "bash": _strings(cap.get("bash"), "bash", name),
            "read_paths": _strings(cap.get("read_paths"), "read_paths", name),
            # Separate from read_paths on purpose: with one list, a
            # caller who may Write anywhere they may Read could overwrite the very
            # script their `bash` allowlist lets them run (`bin/ace-email`), and
            # then run it. The guard refuses Write/Edit outside these; none → no
            # writes at all.
            "write_paths": _strings(cap.get("write_paths"), "write_paths", name),
            **_sites(cap, name),
        }
        if cap.get("data") is not None:
            out[name]["data"] = _data(cap["data"], name, out[name])
    if ignored:
        logger.warning("interface: ignoring retired field(s) %s", ", ".join(ignored))
    return {"capabilities": out}


_SITE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
#: An app key (`apps/workspace_apps/models.py::AppInstallation.key`).
_APP_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _entry_command(entry, name: str):
    if entry is not None and (not isinstance(entry, str) or "\n" in entry or len(entry) > _MAX_PATTERN
                              or invocations.parse(entry) is None):
        raise InterfaceError(f"{name}.entry must be one plugin command: /<plugin>:<skill> [args]")
    return entry


def _app_entry(cap: dict, name: str) -> dict:
    """An entry bound to ONE workspace app (`apps/agents/app_entries.py`): `apps: [<key>]`, an optional
    `ceiling` that only narrows canopy's fixed tools, and its words. Anything a profile would list for itself
    is refused: canopy fixes an app entry's tools, so the owner cannot widen it. Whether the app is installed
    is the writer's question (`profile_settings.set_app_entry`): a document stays valid when an app goes."""
    from . import app_entries  # noqa: PLC0415 — app_entries reads no models; kept beside its one caller

    bad = set(cap) - app_entries.KEYS - {"input"}
    if bad:
        raise InterfaceError(f"{name}: an app entry's tools are canopy's own and cannot be listed "
                             f"({sorted(bad)}); `ceiling` can only narrow them", f"{name}.{sorted(bad)[0]}")
    apps = _strings(cap.get("apps"), "apps", name)
    if len(apps) != 1:
        raise InterfaceError(f"{name}.apps names exactly one app", f"{name}.apps")
    if not _APP_KEY.match(apps[0]):
        raise InterfaceError(f"{name}.apps: {apps[0]!r} is not an app key", f"{name}.apps")
    ceiling = _strings(cap.get("ceiling"), "ceiling", name)
    unknown = sorted(set(ceiling) - set(app_entries.NARROWABLE))
    if unknown:
        raise InterfaceError(f"{name}.ceiling: {unknown} — an app entry's ceiling names only "
                             f"{', '.join(app_entries.NARROWABLE)}", f"{name}.ceiling")
    return {"description": str(cap.get("description") or "")[:500],
            "entry": _entry_command(cap.get("entry"), name),
            "apps": apps, "ceiling": sorted(set(ceiling))}


def _data(value, name: str, cap: dict) -> str:
    """`data:` — what a profile may serve. `sensitive` is an author's
    declaration that it handles sensitive data, refused when its profile shows it
    cannot (`apps/agents/sensitive_data.py`)."""
    from . import sensitive_data

    if value not in sensitive_data.DATA_VALUES:
        raise InterfaceError(f"{name}.data must be one of {sorted(sensitive_data.DATA_VALUES)}")
    refusals = sensitive_data.structural_refusals(name, cap)
    if refusals:
        raise InterfaceError("; ".join(refusals))
    return value


def _sites(cap: dict, name: str) -> dict:
    """`sites:` (Host site names) and `ceiling:` (tool globs), validated.

    A tool limit with no site is refused: it would read as a grant and apply to
    nothing. A site with no tool limit defers to the host: what the visitor's grant
    reaches is the host's decision (`host_gateway.tool_allowed`).
    """
    sites = _strings(cap.get("sites"), "sites", name)
    for site in sites:
        if not _SITE.match(site):
            raise InterfaceError(f"{name}.sites: {site!r} is not a host site name — "
                                 "letters, digits, hyphens and underscores")
    ceiling = _strings(cap.get("ceiling"), "ceiling", name)
    if ceiling and not sites:
        raise InterfaceError(f"{name}.ceiling needs `sites:` — a tool limit bounds the tools of "
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
            raise InterfaceError(f"{name}.own_login: {site!r} is not one of this profile's "
                                 "`sites:` — it names the agent's login at a site it serves")
        listed = _strings(names, f"own_login.{site}", name)
        for n in listed:
            if not _CREDENTIAL_NAME.match(n):
                raise InterfaceError(f"{name}.own_login.{site}: {n!r} is not a credential name "
                                     "(an environment variable name, e.g. LABS_MCP_TOKEN)")
        if listed:
            out[site] = sorted(set(listed))
    return out


def _declared_resource(turn) -> str:
    """What the page attached to this turn's conversation says it is showing. A fact about the
    conversation that its Session Context shows; it selects no profile."""
    return session_resource(getattr(turn, "chat_session", None))


def session_resource(session) -> str:
    state = getattr(session, "page_state", None) or {} if session is not None else {}
    resource = state.get("resource")
    return resource if isinstance(resource, str) else ""


def _conversation_site(turn) -> str:
    """The host site this turn's conversation is held on — the session's
    server-owned `embed_app`, stamped from the token that created it, never from
    anything the page or the caller sent."""
    return session_site(getattr(turn, "chat_session", None))


def session_site(session) -> str:
    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    site = meta.get("embed_app")
    return site.strip() if isinstance(site, str) else ""


def names_site(cap: dict | None, site: str | None) -> bool:
    """Does this profile list `site` in `sites:`? The one test every reader of a host-site entry asks."""
    return bool(site) and site in ((cap or {}).get("sites") or [])


def entry_for_site(iface: dict | None, site: str | None) -> str | None:
    """The name of the one host-site entry that lists `site`, or None. One entry per site is the writer's rule
    (`profile_settings.document_problems`, which also keeps sites off read mode), and migration `agents/0132` refused to
    deploy over a document that broke it, so there is no second entry to combine."""
    caps = (iface or {}).get("capabilities") or {}
    return next((n for n in sorted(caps) if names_site(caps[n], site)), None)


def site_entry(site: str, iface: dict, opens) -> str | None:
    """The host site whose `sites` include the site this conversation is on.

    Tried before falling back to read mode. A profile the caller cannot open is
    skipped, not fatal: read mode may still admit them.
    """
    if not site:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        if names_site(cap, site) and opens(name, cap):
            return name
    return None


# --- the sites an agent serves on pages ------------------------------------------
#
# An agent is set up for a host site when some profile of its published
# interface lists the site in `sites:`. A conversation held on that site's page
# (`embed_app`) then reaches the site ONLY as the person who sent, under their
# act-as-you permission for the page (their host grant, `host_grants.lookup`) —
# whoever they are, the agent's owner and admins included — and never with the
# agent's own login there (`own_login:`), which such a turn does not hold.


def page_sites(agent) -> set[str]:
    """The host sites `agent` serves on pages: every site some profile of its
    interface lists in `sites:`."""
    caps = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).values()
    return {site for cap in caps for site in (cap.get("sites") or [])}


def _cap_for_site(agent, site: str) -> dict:
    """The entry that lists `site` (`entry_for_site`), or {}."""
    iface = getattr(agent, "interface", None) or {}
    name = entry_for_site(iface, site)
    return (iface.get("capabilities") or {}).get(name) or {} if name else {}


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
    ceiling of the host site that lists it, or none (the host's grant decides)."""
    return list(_cap_for_site(agent, site).get("ceiling") or [])


def own_login_of(agent, site: str) -> list[str]:
    """The names of the agent's own credentials that are its login at `site`
    (`own_login:` of the host site that lists it). Withheld from every turn
    held on that site's page."""
    return sorted(set((_cap_for_site(agent, site).get("own_login") or {}).get(site) or []))


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
    """Whether the agent has published a profile. An interface with none says nothing
    and is treated as none."""
    return bool(iface.get("capabilities"))


def is_review_turn(turn) -> bool:
    """Whether `turn` belongs to a chat opened by asking for a one-session review."""
    from apps.canopy_sessions import access as session_access  # noqa: PLC0415 — access imports the agents app

    return session_access.is_review_conversation(session_access.turn_conversation(turn))


def grant_facts(turn, agent, *, context=None) -> dict:
    """The envelope's account of the asker's standing, from the Turn Context: `granted_by`
    (one word: owner | admin | system | session:<role> | grant:<line> | capability:<name> | refused), `role` (where they stand on the agent's role ladder, None
    when refused), `source` (what put them there: owner | admin | system |
    capability:<name> | session | list:<access-list line>), `permissions` (what they hold on
    THIS turn, sorted) and `manual_only`. `context`: the turn's, when the caller already holds it."""
    from . import turn_context  # cycle: agents.turn_context -> agents.published_profiles  # noqa: PLC0415

    d = context if context is not None else turn_context.of_turn(turn, agent)
    return {"granted_by": d.basis, "role": d.standing.agent_role, "source": d.standing.source,
            "permissions": d.held_permissions, "manual_only": bool(d.review.manual_only)}


def reachable(permissions, iface: dict | None) -> list[str]:
    """The profile names someone confined by `permissions` can use: the published ones their
    lines open, plus read mode itself (`read`) when they hold it and the agent published no read
    mode of its own, since read mode exists on every agent. One list for the roster, the topology and the
    extended card, so none reads "published nothing" as "offers nothing"."""
    caps = (iface or {}).get("capabilities") or {}
    names = [n for n in caps if opens(permissions, n)]
    if read_mode_override(iface) is None and opens(permissions, READ_MODE_NAME):
        names.append(READ_MODE_NAME)
    return sorted(names)


def offered_to(user, agent) -> list[str]:
    """The profiles `user` may use on `agent` directly — their extended A2A card.

    Everything for anyone whose turns run in the whole agent anyway (owner, admins, editors);
    for anyone else, the profiles their role on the agent's access list may use. Nothing
    for someone the list does not name.
    """
    iface = getattr(agent, "interface", None) or {}
    caps = iface.get("capabilities") or {}
    if not getattr(user, "is_authenticated", False):
        return []
    from . import turn_context  # cycle: agents.turn_context -> agents.published_profiles  # noqa: PLC0415

    d = turn_context.for_asker(agent, user, verified=True)
    if d.access == turn_context.FULL:
        return sorted(caps)
    if d.access == turn_context.CONFINED:
        return reachable(d.standing.permissions, iface)
    return []


def profile(agent, name: str) -> dict | None:
    """The profile as the runner and the guard see it, or None for FULL.

    A profile that has since been unpublished yields an EMPTY profile —
    deny everything — never the full one: a turn that was restricted when it
    was queued must not become unrestricted because the file changed.
    """
    if name == FULL_STAMP:
        return None
    if name == SESSION_REVIEW:
        return {"name": SESSION_REVIEW, "entry": None, "tools": list(SESSION_REVIEW_TOOLS), "bash": [],
                "description": f"{agent.slug} reviews one session someone asked it to, and replies to them.",
                "read_paths": ["{cwd}/**"], "write_paths": []}
    _published, cap = published_profile(getattr(agent, "interface", None), name)
    from . import app_entries  # noqa: PLC0415 — app_entries reads no models; kept beside its callers

    if app_entries.is_app_entry(cap):
        # Canopy's fixed tools for one app, never a list the owner wrote (`app_entries.profile`).
        return app_entries.profile(agent, name, cap)
    default_version = None
    if cap is None and is_read_mode(name):
        # Read mode on an agent that published no read mode of its own: canopy's own default, built from the agent
        # as it stands now (a skill added or removed changes the next turn, never a stored one).
        cap, default_version = default_read_profile(agent), READ_PROFILE_VERSION
    cap = cap or {"description": "", "entry": None,
                  "tools": [], "bash": [], "read_paths": [], "write_paths": [],
                  "sites": [], "ceiling": []}
    out = {"name": name, **{k: cap.get(k) for k in
                                  ("description", "entry", "tools", "bash", "read_paths",
                                   "write_paths")}}
    if default_version is not None:
        # Which pinned default this turn ran under (`tests/test_default_read_profile.py`).
        out["default_profile_version"] = default_version
    if cap.get("data"):
        out["data"] = cap["data"]
    if cap.get("sites"):
        # A host site's profile reaches its host ONLY through canopy's gateway, so
        # the gateway is part of the profile by construction — an owner cannot
        # publish a host site and forget the one tool that opens it. The host's
        # own MCP server is not added: a visitor turn does not call it directly.
        # The page's own actions likewise: they are the page's, not the host's
        # (`site_call` refuses them by name and points here).
        out["tools"] = list(dict.fromkeys([*(out.get("tools") or []), *GATEWAY_TOOLS,
                                           *PAGE_TOOLS]))
    return out
