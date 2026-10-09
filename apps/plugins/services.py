"""Each workspace's agent plugins, served as a Claude Code marketplace (canopy-web#1376).

    GET /w/<ws>/marketplace.json                 -> the catalog (this module's `marketplace`)
    GET /w/<ws>/plugins/<agent>/<short-sha>.zip  -> one archive (`archive_for`)

WHY canopy-web AND NOT GITHUB ALONE. Agent repos are private `dimagi-internal`
repos; Claude Code fetches a `github` plugin source with the user's own `git`,
so a member with no GitHub account (a family workspace, a partner) could never
install one. An `archive` source is a zip over HTTPS that needs no git account,
pinned by `sha256`, and authenticated with the same canopy token the canopy
plugin already sends (docs/plugin-marketplace.md has the contract).

GITHUB STAYS THE SOURCE OF TRUTH. An archive is built from the agent repo at the
head of `Agent.repo_ref` (default `main`), i.e. a MERGED commit. Agents keep
shipping branch -> PR -> merge; the next marketplace read after the head moves
(bounded by HEAD_TTL) builds the new archive, and Claude Code sees a new version.

WHOSE GITHUB CREDENTIAL. The agent's owner's — the same identity the agent
already acts on GitHub with: the owner's delegation (`AgentDelegation`, a
fine-grained PAT scoped to the agent's repo), else the owner's GitHub App
connection, else none (a public repo still builds). The person ASKING never
lends anything: reading a plugin is gated by workspace membership, not GitHub.

THE ARCHIVE. GitHub's zipball, repackaged so the plugin root is the zip root
(Claude Code accepts the root at the top or one directory down; we normalise it
so a plugin in a repo subdirectory works too), with fixed timestamps, and with
`plugin.json`'s `version` rewritten to `<repo version>+<short sha>`. That last
part is what makes "a merge publishes a new version" true: Claude Code updates
an installed plugin only when its computed version changes, plugin.json's
version wins over the marketplace entry's, and an agent that merges without
bumping its version would otherwise strand every user on the old copy.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import posixpath
import time
import zipfile
from dataclasses import dataclass

import requests
from django.core.cache import cache
from django.db import IntegrityError, transaction

from apps.agents.delegations import agent_repo, delegation_for
from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret
from apps.workspaces import services as wsvc

from .models import PluginArchive

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 20
#: How long a resolved branch head is trusted before GitHub is asked again.
#: The marketplace is read on `/plugin marketplace update` and by background
#: auto-update; a merge reaches users within this window.
HEAD_TTL = 300
#: Claude Code gives a `marketplace.json` fetch 10 seconds. Building a new
#: archive (one zipball download) usually takes 1-3s; past this budget an agent
#: whose new head is not built yet is served at its last built version, and the
#: next read builds it. `manage.py build_plugin_archives` pre-warms.
BUILD_BUDGET_SECONDS = 6.0
#: Claude Code's own ceiling on an archive download is 256 MiB; stay well inside.
MAX_ZIPBALL_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 50_000
#: Fixed timestamp for every entry, so a rebuild of the same commit is the same
#: bytes on the same zlib (the stored row is the real guarantee of a stable sha).
_EPOCH = (1980, 1, 1, 0, 0, 0)


class BuildError(Exception):
    """This agent's plugin cannot be built; the message says why."""


@dataclass(frozen=True)
class Built:
    content: bytes
    plugin_name: str
    version: str
    description: str
    subdir: str


# ---- GitHub ------------------------------------------------------------------

def github_token(agent: Agent) -> str:
    """The agent owner's GitHub credential, or "" (anonymous: public repos only)."""
    row = delegation_for(agent)
    if row is not None:
        try:
            return decrypt_secret(row.secret_enc)
        except Exception:  # noqa: BLE001 — an undecryptable row is "no delegation"
            logger.warning("plugins: undecryptable GitHub delegation for %s", agent.slug)
    if agent.owner_id:
        from apps.tokens import github_app

        if github_app.is_configured():
            try:
                return github_app.access_token_for(agent.owner)
            except Exception:  # noqa: BLE001 — not connected / refresh failed: fall through
                pass
    return ""


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def resolve_head(repo: str, ref: str, token: str) -> str:
    """The commit `ref` points at now, as a 40-char sha."""
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/commits/{ref}",
        headers=_headers(token, "application/vnd.github.sha"), timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise BuildError(f"GitHub answered {resp.status_code} resolving {repo}@{ref}")
    sha = (resp.text or "").strip()
    if len(sha) != 40:
        raise BuildError(f"GitHub returned no commit for {repo}@{ref}")
    return sha


def download_zipball(repo: str, sha: str, token: str) -> bytes:
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/zipball/{sha}",
        headers=_headers(token), timeout=HTTP_TIMEOUT * 3, stream=True,
    )
    if resp.status_code != 200:
        raise BuildError(f"GitHub answered {resp.status_code} downloading {repo}@{sha[:12]}")
    buf = io.BytesIO()
    for chunk in resp.iter_content(1024 * 256):
        buf.write(chunk)
        if buf.tell() > MAX_ZIPBALL_BYTES:
            raise BuildError(f"{repo} is larger than {MAX_ZIPBALL_BYTES // 2**20} MiB")
    return buf.getvalue()


# ---- repackaging ---------------------------------------------------------------

def _safe(name: str) -> bool:
    return not (name.startswith("/") or "\\" in name or ".." in name.split("/"))


def _plugin_root(names: set[str], top: str, read, agent_slug: str) -> str:
    """The plugin root inside the zipball, as a prefix ending in "/"."""
    if f"{top}.claude-plugin/plugin.json" in names:
        return top
    # A marketplace repo whose plugin lives in a subdirectory (`./plugins/x`).
    mp = f"{top}.claude-plugin/marketplace.json"
    if mp in names:
        try:
            entries = json.loads(read(mp)).get("plugins") or []
        except (ValueError, AttributeError) as exc:
            raise BuildError(f"unreadable .claude-plugin/marketplace.json: {exc}") from exc
        local = [e for e in entries if isinstance(e, dict) and isinstance(e.get("source"), str)
                 and e["source"].startswith("./")]
        chosen = next((e for e in local if e.get("name") == agent_slug), None)
        if chosen is None and len(local) == 1:
            chosen = local[0]
        if chosen is not None:
            sub = posixpath.normpath(chosen["source"][2:] or ".")
            root = top if sub == "." else f"{top}{sub}/"
            if _safe(sub) and f"{root}.claude-plugin/plugin.json" in names:
                return root
    raise BuildError("the repo has no .claude-plugin/plugin.json at its root "
                     "(nor one marketplace plugin with a local source)")


def repackage(zipball: bytes, commit_sha: str, agent_slug: str = "") -> Built:
    """GitHub's zipball -> the archive Claude Code installs. See the module docstring."""
    try:
        src = zipfile.ZipFile(io.BytesIO(zipball))
    except zipfile.BadZipFile as exc:
        raise BuildError(f"GitHub's zipball is not a zip: {exc}") from exc
    infos = src.infolist()
    if len(infos) > MAX_ENTRIES:
        raise BuildError(f"the repo has more than {MAX_ENTRIES} files")
    names = {i.filename for i in infos}
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) != 1:
        raise BuildError("GitHub's zipball has no single top-level directory")
    top = tops.pop() + "/"
    root = _plugin_root(names, top, src.read, agent_slug)

    manifest_name = f"{root}.claude-plugin/plugin.json"
    try:
        manifest = json.loads(src.read(manifest_name))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        raise BuildError(f"unreadable .claude-plugin/plugin.json: {exc}") from exc
    plugin_name = str(manifest.get("name") or agent_slug or "").strip()
    if not plugin_name:
        raise BuildError(".claude-plugin/plugin.json has no name")
    short = commit_sha[:12]
    base = str(manifest.get("version") or "").strip()
    version = f"{base.split('+', 1)[0]}+{short}" if base else f"0.0.0+{short}"
    manifest["version"] = version
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info in sorted(infos, key=lambda i: i.filename):
            if not info.filename.startswith(root):
                continue
            rel = info.filename[len(root):]
            if not rel or not _safe(rel):
                continue
            zi = zipfile.ZipInfo(rel, date_time=_EPOCH)
            # Keep the mode bits, which is what keeps an executable script
            # executable and a symlink a symlink.
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            if info.is_dir():
                dst.writestr(zi, b"")
                continue
            zi.compress_type = zipfile.ZIP_DEFLATED
            body = manifest_bytes if info.filename == manifest_name else src.read(info)
            dst.writestr(zi, body)
    return Built(
        content=out.getvalue(), plugin_name=plugin_name, version=version,
        description=str(manifest.get("description") or ""),
        subdir=root[len(top):].rstrip("/"),
    )


# ---- building + caching ------------------------------------------------------------

def _head_key(repo: str, ref: str) -> str:
    return f"plugins:head:{repo}:{ref}"


def current_head(agent: Agent, token: str | None = None) -> str:
    """The agent repo's head sha, trusted for HEAD_TTL seconds."""
    repo, ref = agent_repo(agent), (agent.repo_ref or "main")
    key = _head_key(repo, ref)
    sha = cache.get(key)
    if sha:
        return sha
    sha = resolve_head(repo, ref, github_token(agent) if token is None else token)
    cache.set(key, sha, HEAD_TTL)
    return sha


def build(agent: Agent, commit_sha: str, token: str | None = None) -> PluginArchive:
    """The archive for `agent`'s repo at `commit_sha`, built now if it isn't stored."""
    repo = agent_repo(agent)
    existing = PluginArchive.objects.filter(repo=repo, commit_sha=commit_sha).first()
    if existing is not None:
        return existing
    zipball = download_zipball(repo, commit_sha, github_token(agent) if token is None else token)
    built = repackage(zipball, commit_sha, agent.slug)
    try:
        with transaction.atomic():
            return PluginArchive.objects.create(
                repo=repo, commit_sha=commit_sha, subdir=built.subdir,
                plugin_name=built.plugin_name, version=built.version,
                description=built.description,
                sha256=hashlib.sha256(built.content).hexdigest(),
                size_bytes=len(built.content), content=built.content,
            )
    except IntegrityError:  # a concurrent build of the same commit won
        return PluginArchive.objects.get(repo=repo, commit_sha=commit_sha)


def latest_built(repo: str) -> PluginArchive | None:
    return PluginArchive.objects.filter(repo=repo).order_by("-created_at").first()


def archive_for_listing(agent: Agent, *, may_build: bool) -> PluginArchive | None:
    """What the marketplace lists for `agent`: its head's archive, built if
    allowed; else the newest one already built; else nothing."""
    repo = agent_repo(agent)
    try:
        token = github_token(agent)
        sha = current_head(agent, token)
        stored = PluginArchive.objects.filter(repo=repo, commit_sha=sha).first()
        if stored is not None:
            return stored
        if may_build:
            return build(agent, sha, token)
    except (BuildError, requests.RequestException) as exc:
        logger.warning("plugins: %s (%s) not built: %s", agent.slug, repo, exc)
    return latest_built(repo)


def workspace_agents(workspace) -> list[Agent]:
    """The workspace's agents that have a github.com repo, in a stable order."""
    agents = Agent.objects.filter(workspace=workspace).exclude(repo_url="").order_by("slug")
    return [a for a in agents if agent_repo(a)]


def archive_url(workspace, agent: Agent, archive: PluginArchive) -> str:
    return wsvc.scoped_url(workspace, f"/plugins/{agent.slug}/{archive.short_sha}.zip")


def marketplace_name(workspace) -> str:
    return f"canopy-{workspace.slug}"


def marketplace(workspace, *, budget: float = BUILD_BUDGET_SECONDS) -> dict:
    """The workspace's `marketplace.json`, as a dict."""
    deadline = time.monotonic() + budget
    plugins, seen = [], set()
    for agent in workspace_agents(workspace):
        archive = archive_for_listing(agent, may_build=time.monotonic() < deadline)
        if archive is None or archive.plugin_name in seen:
            continue
        seen.add(archive.plugin_name)
        plugins.append({
            "name": archive.plugin_name,
            "description": archive.description or agent.description or agent.name,
            "version": archive.version,
            "source": {
                "source": "archive",
                "url": archive_url(workspace, agent, archive),
                "sha256": archive.sha256,
            },
            "metadata": {
                "agent": agent.slug,
                "repo": archive.repo,
                "commit": archive.commit_sha,
            },
        })
    display = workspace.display_name or workspace.slug
    return {
        "name": marketplace_name(workspace),
        "owner": {"name": display, "url": wsvc.scoped_url(workspace, "/agents")},
        "description": f"The agents of the {display} workspace on canopy, as Claude Code plugins.",
        "plugins": plugins,
    }


def archive_for(workspace, agent_slug: str, short_sha: str) -> PluginArchive | None:
    """The stored archive an archive URL names, if it belongs to an agent of
    `workspace`. Never builds: every URL the marketplace hands out names a row
    that already exists."""
    if not (7 <= len(short_sha) <= 40) or any(c not in "0123456789abcdef" for c in short_sha):
        return None
    agent = Agent.objects.filter(workspace=workspace, slug=agent_slug).first()
    if agent is None or not agent_repo(agent):
        return None
    return (PluginArchive.objects.filter(repo=agent_repo(agent), commit_sha__startswith=short_sha)
            .order_by("-created_at").first())
