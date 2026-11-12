"""An agent's APP ENTRIES: a profile bound to ONE workspace app of the agent's own workspace.

An app entry is one entry of the agent's published document (`Agent.interface["capabilities"]`), like
read mode or a host site, that names an app instead of a site:

    capabilities:
      household:
        apps: [household]                  # exactly one app, installed in the agent's workspace
        description: Ask Bailey about the household chores.
        entry: /bailey:household-ask --thread {thread_id}   # optional
        # ceiling: [app_tasks_query, app_tasks_act]          # optional: only NARROWS the fixed tools

**Its tools are canopy's, never the owner's** (`profile`): `who_is_asking`, `current_page`, the page's own
actions (`page_*`), and `app_query` / `app_act` / `app_tasks_query` / `app_tasks_act`
(`apps/mcp/tools/app.py`), whose app is resolved from the TURN's entry (`Turn.capability` → this entry →
its app), never an argument. Plus `Read`/`Write` in a scratch folder only, and, on an agent with a
mailbox, the same `{thread_id}`-pinned email read and reply read mode uses (`read_mode.default_read_profile`)
and nothing else of its mail. No other Bash, no Read outside scratch, no other canopy tool. `ceiling` names
which of the narrowable tools stay; it can only take some away.

**Who reaches it is the access list** (`use:<key>`, `apps/agents/grants.py`); selection never grants
(`turn_context._entry_for`). A conversation created from the app's own chat panel is stamped
`metadata.workspace_app = "<ws>/<key>"` by the server (`WORKSPACE_APP_KEY`), which SELECTS the entry for
anyone without `write` whose lines open it; a `read` line opens the app entry of the conversation it is
stamped with (and read mode everywhere else).

**The confined turn writes as its asker, within the app only.** `app_act` / `app_tasks_act` run the app's
DECLARED actions as the turn's verified, active human asker: their own app role decides, and history records
"<agent> for <person>". This is the one narrow exception to "an agent turn's caller token never writes"
(docs/architecture/access-confinement-and-act-as.md § App entries).

The workspace-apps product app is one this framework app cannot import (ARCHITECTURE.md), so what an app IS
(installed? may this person open it? query and act) is asked through the `Host` it registers in its
`AppConfig.ready()` (`register`). No host registered: no app exists, and every app tool refuses.
"""
from __future__ import annotations

from typing import Protocol

#: `Session.metadata` key, SERVER-OWNED (stamped from the panel's token by
#: `canopy_sessions.services.stamp_host_metadata`, never from a body): `"<workspace>/<app key>"`.
WORKSPACE_APP_KEY = "workspace_app"

#: canopy's app-scoped tools (`apps/mcp/tools/app.py`), by their bare MCP names.
APP_TOOLS = ("app_query", "app_act", "app_tasks_query", "app_tasks_act")
#: What `ceiling` may name: the app tools and the page's (the rest of the profile is the minimum a turn needs).
NARROWABLE = (*APP_TOOLS, "current_page", "page_*")
#: The keys an app entry may carry. Anything a profile lists for itself (`tools`, `bash`, paths, `sites`,
#: `data`, `own_login`) is refused: canopy fixes an app entry's tools.
KEYS = frozenset({"apps", "ceiling", "description", "entry"})

_SERVER = "mcp__*canopy-web__"


class Refused(Exception):
    """An app tool refused, in words fit to give the agent (and through it, the person)."""


def is_app_entry(cap: dict | None) -> bool:
    return bool((cap or {}).get("apps"))


def app_of(cap: dict | None) -> str | None:
    """The one app key an entry names, or None for any other entry."""
    apps = (cap or {}).get("apps") or []
    return apps[0] if apps else None


def entries(iface: dict | None) -> dict:
    """`{entry key: app key}` for every app entry of a document."""
    caps = (iface or {}).get("capabilities") or {}
    return {name: app_of(cap) for name, cap in caps.items() if is_app_entry(cap)}


def entry_for_app(iface: dict | None, app_key: str | None) -> str | None:
    """The name of the one entry bound to `app_key`, or None (one entry per app: the writer's rule)."""
    if not app_key:
        return None
    return next((n for n, k in sorted(entries(iface).items()) if k == app_key), None)


def app_of_entry(iface: dict | None, name: str | None) -> str | None:
    """The app key of the entry `name`, or None when it is not an app entry (or is gone)."""
    caps = (iface or {}).get("capabilities") or {}
    return app_of(caps.get(name)) if name else None


def session_app(session) -> str:
    """`"<workspace>/<key>"` this conversation was created from (its server-owned stamp), or ""."""
    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    value = meta.get(WORKSPACE_APP_KEY)
    return value.strip() if isinstance(value, str) else ""


def stamp_of(workspace: str, key: str) -> str:
    return f"{workspace}/{key}"


def split(stamp: str) -> tuple[str, str]:
    workspace, _, key = (stamp or "").partition("/")
    return workspace, key


def selected_entry(agent, session_app_stamp: str) -> str | None:
    """The app entry a conversation stamped `"<ws>/<key>"` selects on `agent`: only an app of the agent's
    own workspace, and only when the agent has an entry for it. Selects; never grants."""
    workspace, key = split(session_app_stamp)
    if not key or workspace != getattr(agent, "workspace_id", None):
        return None
    return entry_for_app(getattr(agent, "interface", None), key)


def scratch(agent, name: str) -> str:
    return f"{{cwd}}/.{agent.slug}-{name}/*"


def tools_for(cap: dict | None) -> list[str]:
    """The canopy tools an app entry holds: every narrowable tool, cut to its `ceiling` when it has one."""
    ceiling = set((cap or {}).get("ceiling") or [])
    kept = [t for t in NARROWABLE if not ceiling or t in ceiling]
    return ["Read", "Write", "ToolSearch", f"{_SERVER}who_is_asking", *(f"{_SERVER}{t}" for t in kept)]


def profile(agent, name: str, cap: dict) -> dict:
    """The profile as the runner and the guard read it: canopy's fixed tools, scratch-only paths, and on an
    agent with a mailbox the thread-pinned email read and reply read mode uses, writing its drafts in this
    entry's scratch folder. Nothing the owner lists widens it."""
    folder = scratch(agent, name)
    bash: list[str] = []
    if (getattr(agent, "email", "") or "").strip():
        bash = ["canopy email read --repo . {thread_id}",
                f"canopy email review-receipt --repo . --body-file {folder}",
                f"bin/{agent.slug}-email --reply-all --thread-id {{thread_id}} "
                f"--subject-file {folder} --body-file {folder}"]
    return {
        "name": name,
        "description": str(cap.get("description") or ""),
        "entry": cap.get("entry"),
        "tools": tools_for(cap),
        "bash": bash,
        "read_paths": [folder],
        "write_paths": [folder],
    }


# --- what an app is: asked of the product app that owns apps ---------------------------------------

class Host(Protocol):
    """What the workspace-apps product app answers for an app entry. Every method takes the app by
    `(workspace, key)`; the person is a canopy user; `agent` is who acts FOR them (history records
    "<agent> for <person>"). Each raises `Refused` for anything the person may not do, and `ValueError`
    for input the app does not take."""

    def installed(self, workspace: str, key: str) -> bool: ...

    def available(self, workspace: str) -> list[str]: ...

    def name(self, workspace: str, key: str) -> str: ...

    def may_open(self, user, workspace: str, key: str) -> bool: ...

    def describe(self, workspace: str, key: str, user) -> dict: ...

    def query(self, workspace: str, key: str, user, *, collection: str, where: dict | None, order: str,
              limit: int) -> list[dict]: ...

    def act(self, workspace: str, key: str, user, agent, *, action: str, record: int | None,
            input: dict | None, params: dict | None) -> dict: ...

    def tasks_query(self, workspace: str, key: str, user, **filters) -> list[dict]: ...

    def tasks_act(self, workspace: str, key: str, user, agent, *, name: str, task: str | None,
                  input: dict | None) -> dict: ...


_HOST: Host | None = None


def register(host: Host) -> None:
    """Called once, from its `AppConfig.ready()`, by the product app that owns workspace apps."""
    global _HOST
    _HOST = host


def host() -> Host | None:
    return _HOST


def installed(workspace: str, key: str) -> bool:
    return _HOST is not None and _HOST.installed(workspace, key)


def may_open(user, workspace: str, key: str) -> bool:
    """May `user` open the app's pages — a member of its workspace, or a guest whose scope names it?"""
    return _HOST is not None and getattr(user, "is_authenticated", False) and _HOST.may_open(user, workspace, key)
