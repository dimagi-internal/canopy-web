"""One personal Claude Code catalog of canopy agents (docs/architecture/plugin-marketplace.md).

    GET /plugins/marketplace.json  -> `catalog(user)`: marketplace `canopy-agents`

ONE CATALOG PER PERSON, NOT PER WORKSPACE (Jonathan, 2026-10-14). It covers every
agent with a github.com repo in every workspace the caller is a member of, at
any role, across all their organisations, so the install name does not depend
on which workspace an agent lives in, and survives the agent moving.

PLUGINS, NOT INSTANCES. The catalog lists CODE. Two `Agent` rows (in two
workspaces, with different slugs) that point at the same repo + ref are two
instances of ONE plugin; installing it twice would load the same skills and MCP
servers twice under one plugin.json name. So entries are keyed by the plugin
source (repo, ref), and named from the plugin's own `plugin.json` name (falling
back to the repo name), never from `Agent.slug`. Which instance a person works
with (workspace, vault, config) is the agent's own setup command's job.

CANOPY NEVER LENDS GITHUB ACCESS. Each entry is a `github` plugin source, which
Claude Code fetches with the installing person's OWN git credentials. canopy
only decides which plugins to LIST; whether you can INSTALL one is decided by
GitHub — you need read access to the agent's repo. The only GitHub read canopy
makes is an ANONYMOUS fetch of `plugin.json` for the entry's name (it succeeds
for a public repo; a private one falls back to the repo name).

UPDATES. Claude Code tracks the repo itself: it computes a plugin's version from
`plugin.json`'s `version`, else the entry's, else the commit sha, and updates an
installed copy only when that changes. This module deliberately sets no entry
`version`, so an agent that bumps `plugin.json` on merge publishes the update;
one that merges without bumping strands users on their cached copy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests
from django.core.cache import cache

from apps.agents.delegations import agent_repo
from apps.agents.models import Agent
from apps.workspaces import services as wsvc
from apps.workspaces import links as wslinks

logger = logging.getLogger(__name__)

MARKETPLACE_NAME = "canopy-agents"
#: The path Claude Code fetches. Not `/agents/…`: that is the SPA's
#: `/agents/*` → `/w/<ws>/agents` redirect (router.tsx, PWA allowlist).
CATALOG_PATH = "/plugins/marketplace.json"
#: The ref canopy assumes when `Agent.repo_ref` is blank; a source omits `ref`
#: for it, so Claude Code follows the repo's default branch.
DEFAULT_REF = "main"
#: Anonymous, so it never borrows anyone's GitHub access. Short, because Claude
#: Code gives a marketplace.json fetch 10 seconds.
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/.claude-plugin/plugin.json"
NAME_TIMEOUT = 2.0
NAME_TTL = 3600
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def catalog_url() -> str:
    return wslinks.public_url(CATALOG_PATH)


def member_agents(user) -> list[Agent]:
    """Agents with a github.com repo in any workspace `user` belongs to, oldest first.

    Membership is `user_workspace_slugs`: direct memberships at any role, plus
    the descendants of workspaces they own (the only access the tree grants)."""
    slugs = wsvc.user_workspace_slugs(user)
    if not slugs:
        return []
    agents = (Agent.objects.filter(workspace_id__in=slugs).exclude(repo_url="")
              .order_by("created_at", "slug"))
    return [a for a in agents if agent_repo(a)]


def _ref(agent: Agent) -> str:
    ref = (agent.repo_ref or "").strip()
    return "" if ref == DEFAULT_REF else ref


def source_key(agent: Agent) -> tuple[str, str]:
    """What identifies a plugin: its repo and ref ("" = the default branch).
    (`Agent` has no plugin subdirectory; every agent repo has its plugin at the root.)"""
    return agent_repo(agent).lower(), _ref(agent)


def plugin_source(agent: Agent) -> dict:
    """The `github` source for `agent`'s repo — fetched with the user's own git."""
    source = {"source": "github", "repo": agent_repo(agent)}
    if _ref(agent):
        source["ref"] = _ref(agent)
    return source


def _safe_name(name: str) -> str:
    return _UNSAFE.sub("-", name).strip("-.")


def _raw_get(url: str):
    """The one GitHub read canopy makes: anonymous, short. A seam so the test
    suite never reaches GitHub (conftest `_no_live_plugin_json`)."""
    return requests.get(url, timeout=NAME_TIMEOUT)


def plugin_name(agent: Agent) -> str:
    """The plugin's own `plugin.json` name, read anonymously and cached; else the
    repo name."""
    repo = agent_repo(agent)
    fallback = _safe_name(repo.split("/", 1)[1])
    ref = _ref(agent) or "HEAD"
    key = f"plugins:name:{repo.lower()}:{ref}"
    cached = cache.get(key)
    if cached is not None:
        return cached or fallback
    name = ""
    try:
        resp = _raw_get(RAW_URL.format(repo=repo, ref=ref))
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                name = _safe_name(str(data.get("name") or ""))
    except (requests.RequestException, ValueError):
        pass
    cache.set(key, name, NAME_TTL)
    return name or fallback


@dataclass
class _Plugin:
    name: str
    instances: list[Agent] = field(default_factory=list)

    @property
    def first(self) -> Agent:
        return self.instances[0]


def _qualified(name: str, key: tuple[str, str]) -> str:
    repo, ref = key
    return _safe_name("--".join([name, repo.replace("/", "-")] + ([ref] if ref else [])))


def plugins_for(user) -> list[_Plugin]:
    """One plugin per (repo, ref) the user can see, named from plugin.json.

    Two DIFFERENT sources with the same plugin name (a fork): the source of the
    oldest instance keeps the name; each other one is listed as
    `<name>--<owner>-<repo>[--<ref>]` and logged. Nothing is dropped."""
    by_key: dict[tuple[str, str], _Plugin] = {}
    for agent in member_agents(user):  # oldest first
        key = source_key(agent)
        if key not in by_key:
            by_key[key] = _Plugin(name=plugin_name(agent))
        by_key[key].instances.append(agent)
    taken: dict[str, tuple[str, str]] = {}
    for key, plugin in by_key.items():
        if plugin.name in taken:
            qualified = _qualified(plugin.name, key)
            logger.warning("plugins: %s is also %s; listing %s@%s as %s", plugin.name,
                           "/".join(filter(None, taken[plugin.name])), key[0], key[1] or DEFAULT_REF,
                           qualified)
            plugin.name = qualified
        taken[plugin.name] = key
    return sorted(by_key.values(), key=lambda p: p.name)


def entry(plugin: _Plugin) -> dict:
    agent = plugin.first
    return {
        "name": plugin.name,
        "description": agent.description or agent.display_name,
        "source": plugin_source(agent),
        "metadata": {
            # An INSTANCE is canopy-web's Agent row: identity, workspace, and the
            # 1Password vault its setup reads from. `vault` is the vault NAME
            # only (never the service-account token); 1Password decides who can
            # read it. "" when the agent has none recorded.
            "instances": [{"agent": a.slug, "workspace": a.workspace_id, "vault": a.op_vault or ""}
                          for a in sorted(plugin.instances, key=lambda a: a.slug)],
        },
    }


def plugin_refs(user) -> dict[int, dict]:
    """`{agent id: {"repo", "name"}}` for every agent in the user's catalog —
    the SAME names the catalog lists (fork qualification included), so the
    agents API and `marketplace.json` cannot disagree on what to install."""
    return {a.id: {"repo": agent_repo(p.first), "name": p.name}
            for p in plugins_for(user) for a in p.instances}


def plugin_ref(agent: Agent, refs: dict[int, dict] | None = None) -> dict | None:
    """The plugin `agent` is an instance of, as its caller's catalog names it,
    or None when it has no github.com repo (it is not in any catalog)."""
    if refs is not None and agent.id in refs:
        return refs[agent.id]
    if not agent.repo_url or not agent_repo(agent):
        return None
    return {"repo": agent_repo(agent), "name": plugin_name(agent)}


def catalog(user) -> dict:
    """The caller's `marketplace.json`, as a dict. Valid (and empty) for a user
    with no memberships."""
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "canopy", "url": wslinks.public_url("/")},
        "description": ("The canopy agents in your workspaces, as Claude Code plugins. "
                        "Installing one needs your own read access to its GitHub repo."),
        "plugins": [entry(p) for p in plugins_for(user)],
    }
