"""An agent's two plain settings, read mode and host sites, as views of `Agent.interface`.

`docs/superpowers/specs/2026-11-04-agent-settings-replace-the-interface-design.md` § 3. Nothing about
storage moves and no reader changes: this reads the same document everything else reads and edits it
through the one writer, `store`. An edit validates only the entry it changes, with the server's own
parser, and leaves every other entry as it was saved.
"""
from __future__ import annotations

import copy
import re

from django.db import transaction
from django.utils import timezone

from . import interface as iface_mod
from . import sensitive_data
from .models import Agent

#: The fields of a profile, in either setting. A host site adds `ceiling`, `own_login`, `sensitive`.
PROFILE_FIELDS = ("description", "entry", "tools", "bash", "read_paths", "write_paths")
_RESERVED_KEYS = frozenset({*iface_mod.READ_MODE_NAMES, iface_mod.SESSION_REVIEW})
_MAX_ENTRIES = 50


class SettingsError(ValueError):
    """A change the settings do not allow, in words fit to show beside the form."""


def new_site_defaults() -> dict:
    """What a new host site starts with, saved explicitly so a later change here widens nobody.
    Passes `sensitive_data.structural_refusals`: adding a site and switching sensitive on needs no more."""
    return {
        "description": "",
        "entry": None,
        "tools": ["Read", "Skill", "ToolSearch", "mcp__*canopy-web__who_is_asking",
                  "mcp__*canopy-web__current_page"],
        "bash": [],
        "read_paths": ["{cwd}/**"],
        "write_paths": [],
        "ceiling": [],
        "own_login": [],
        "sensitive": False,
    }


def _entries(agent_or_doc) -> dict:
    doc = getattr(agent_or_doc, "interface", agent_or_doc) or {}
    return doc.get("capabilities") or {}


def _profile_of(entry: dict) -> dict:
    return {field: entry.get(field) if field == "entry" else (entry.get(field) or
                                                               ("" if field == "description" else []))
            for field in PROFILE_FIELDS}


# --- reading -----------------------------------------------------------------------------------

def read_mode_of(agent) -> dict:
    """`{source: default|custom, profile}`: what someone who may only read gets from `agent`."""
    name = iface_mod.read_mode_override(agent.interface)
    if name is not None:
        return {"source": "custom", "profile": _profile_of(_entries(agent)[name])}
    return {"source": "default", "profile": _profile_of(iface_mod.default_read_profile(agent))}


def _row(name: str, entry: dict, site: str) -> dict:
    return {"site": site, "key": name, **_profile_of(entry), "ceiling": entry.get("ceiling") or [],
            "own_login": (entry.get("own_login") or {}).get(site) or [],
            "sensitive": entry.get("data") == sensitive_data.SENSITIVE}


def host_sites_of(agent) -> list[dict]:
    """One row per site, by site. Read mode is never a row, even if a stored `READ_MODE_STAMP` entry names a site."""
    rows = [_row(name, entry or {}, site) for name, entry in _entries(agent).items()
            if not iface_mod.is_read_mode(name) for site in (entry or {}).get("sites") or []]
    return sorted(rows, key=lambda r: (r["site"], r["key"]))


def document_problems(caps: dict) -> list[str]:
    """Why a document does not fit the plain settings, each named. Empty: it fits. It keeps working
    either way; this refuses only a save through the new routes, and the snapshot's `shape` reports it."""
    out = []
    by_site: dict[str, list[str]] = {}
    for name, cap in caps.items():
        cap = cap or {}
        sites = cap.get("sites") or []
        for site in sites:
            by_site.setdefault(site, []).append(name)
        if iface_mod.is_read_mode(name):
            if sites or cap.get("ceiling") or cap.get("own_login") or cap.get("data"):
                out.append(f"{name}: read mode names sites, a ceiling, a login or sensitive data")
        elif not sites:
            out.append(f"{name}: has no site")
        elif len(sites) > 1:
            out.append(f"{name}: names {len(sites)} sites")
    out += [f"{site}: named by {', '.join(sorted(names))}" for site, names in by_site.items() if len(names) > 1]
    return sorted(out)


def key_for(site: str, taken) -> str:
    """The internal name for a new site's row: never renamed, because it is what a turn is stamped with
    and what an access line names. Fits `interface.parse`'s name rule, and is never a name canopy
    reserves: not read mode's, and not `session_review`, which nobody can publish."""
    base = re.sub(r"[^a-z0-9_]", "_", site.lower())[:37]
    if not base[:1].isalpha():
        base = f"site_{base}"[:37]
    taken = {*taken, *_RESERVED_KEYS}
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


# --- the one writer ----------------------------------------------------------------------------

def store(agent, parsed: dict, by) -> None:
    """Save a whole document on `agent`. Every interface write ends here."""
    agent.interface = parsed
    agent.interface_published_at = timezone.now()
    agent.interface_published_by = by
    agent.save(update_fields=["interface", "interface_published_at", "interface_published_by", "updated_at"])


def _validated(name: str, entry: dict) -> dict:
    try:
        return iface_mod.parse({"capabilities": {name: entry}})["capabilities"][name]
    except iface_mod.InterfaceError as exc:
        # The parser names the stored document's keys; a person at the form reads the form's words.
        raise SettingsError(re.sub(r" \(`\w+:` must be empty\)", "", str(exc))) from exc


def _edit(agent, change, *, by, dry_run: bool) -> tuple[Agent, dict, dict]:
    """`change(doc)` applied to the agent's current document, under a lock on its row, so two people
    editing different rows cannot lose one. Returns `(agent, entries before, entries after)`."""
    with transaction.atomic():
        locked = Agent.objects.select_for_update().get(pk=agent.pk)
        doc = copy.deepcopy(locked.interface or {})
        doc.setdefault("capabilities", {})
        before = copy.deepcopy(doc["capabilities"])
        # Asked of the document as stored: an edit could otherwise tidy away what it cannot hold, such
        # as the second site of a row it rewrites. A document that fits still fits after any change here.
        problems = document_problems(before)
        if problems:
            raise SettingsError(f"{agent.slug} is set up in a way these settings cannot hold "
                                f"({'; '.join(problems)}). Nothing was changed and the agent keeps "
                                "working as it is. Ask canopy-web's maintainers to correct its saved setup.")
        change(doc)
        if len(doc["capabilities"]) > _MAX_ENTRIES:
            raise SettingsError(f"an agent can hold at most {_MAX_ENTRIES} of these")
        if not dry_run:
            store(locked, doc, by)
    return locked, before, doc["capabilities"]


def set_read_mode(agent, fields: dict, *, by, dry_run: bool = False):
    """Customise read mode: `fields` over what it is now. The first change starts from canopy's default
    as it stands for this agent, and saves it as the agent's own."""
    def change(doc):
        caps = doc["capabilities"]
        name = iface_mod.read_mode_override(doc) or iface_mod.READ_MODE_STAMP
        start = caps.get(name) or _profile_of(iface_mod.default_read_profile(agent))
        caps[name] = _validated(name, {**_profile_of(start), **fields})
    return _edit(agent, change, by=by, dry_run=dry_run)


def reset_read_mode(agent, *, by, dry_run: bool = False):
    """Back to canopy's default: both names go, or the other would take over."""
    def change(doc):
        for name in iface_mod.READ_MODE_NAMES:
            doc["capabilities"].pop(name, None)
    return _edit(agent, change, by=by, dry_run=dry_run)


def _name_for_site(caps: dict, site: str) -> str | None:
    return next((name for name, entry in caps.items()
                 if not iface_mod.is_read_mode(name) and site in ((entry or {}).get("sites") or [])), None)


def _limits_nothing(globs) -> str | None:
    """The first tool-limit glob that matches every tool the site offers, or None.

    `mcp__*connect_labs__*` reads like a restriction and restricts nothing: the host's tool name is
    all that is matched (`host_gateway.host_tool_name`), and `*` is every one of them. ACE's labs
    row carried exactly that until 2026-11-06, which is how a no-op passed for a safety setting.
    """
    from apps.host_grants.host_gateway import host_tool_name  # noqa: PLC0415 — host_grants imports agents

    return next((g for g in globs or [] if host_tool_name(g) == "*"), None)


def set_site(agent, site: str, fields: dict, *, by, dry_run: bool = False):
    """Add or change `site`'s row: `fields` over what it holds now, or over `new_site_defaults`."""
    def change(doc):
        caps = doc["capabilities"]
        name = _name_for_site(caps, site)
        start = _row(name, caps[name] or {}, site) if name else new_site_defaults()
        # Only a limit this save CHANGES is checked: the form sends the stored one back with every
        # save, and a row stored before this guard must stay editable.
        if fields.get("ceiling") is not None and fields["ceiling"] != start["ceiling"]:
            if bad := _limits_nothing(fields["ceiling"]):
                raise SettingsError(
                    f"Site tools it may use: {bad} matches every tool the site offers, so it limits nothing. "
                    "Leave the list empty to let the site's own permission decide, "
                    "or name the tools to keep the agent to."
                )
        merged = {**start, **fields}
        name = name or key_for(site, caps)
        entry = {f: merged[f] for f in PROFILE_FIELDS}
        entry.update(sites=[site], ceiling=merged["ceiling"],
                     own_login={site: merged["own_login"]} if merged["own_login"] else None)
        if merged["sensitive"]:
            entry["data"] = sensitive_data.SENSITIVE
        caps[name] = _validated(name, entry)
    return _edit(agent, change, by=by, dry_run=dry_run)


def remove_site(agent, site: str, *, by, dry_run: bool = False):
    """Take `site`'s row away. A turn already stamped with it gets the deny-all profile, as for any
    profile that is gone, and an access line naming it opens nothing."""
    def change(doc):
        name = _name_for_site(doc["capabilities"], site)
        if name is None:
            raise LookupError(site)
        doc["capabilities"].pop(name)
    return _edit(agent, change, by=by, dry_run=dry_run)
