"""A stored agent's files: where their bytes live, and what the core reads from a revision.

The bytes of every file are content-addressed blobs (`AgentDefinitionBlob`), in the
attachment bucket when one is configured and in the row otherwise — the same split as a
local app's package. This module stores and reads them, renders the files canopy writes
itself, builds a revision's archive, and reads the skill catalog out of a revision. Who
may change a definition, and how (drafts, publish, rollback, approvals), is
`apps/agent_definitions`, one layer up. Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import re
import tarfile

from canopy_agent_factory import AgentSpec
from canopy_sdk import agent_definition as policy

from apps.canopy_sessions import attachment_storage
from apps.workspaces.links import scoped_url

from .creation import render
from .models import Agent, AgentDefinitionBlob, AgentDefinitionRevision, AgentSkill

#: Where blobs go in the attachment bucket.
STORAGE_PREFIX = "agent-definitions"
#: Committer of the deterministic commit runners make (`canopy agent materialize`), and
#: the owner named in the generated plugin manifests.
CANOPY_NAME = "canopy"
CANOPY_EMAIL = "agents@canopy.dimagi.com"


def _storage_key(sha256: str) -> str:
    return f"{STORAGE_PREFIX}/{sha256[:2]}/{sha256}"


def put_blob(data: bytes) -> AgentDefinitionBlob:
    """Store `data` once, by its sha256; an existing blob is returned as it is."""
    sha = policy.sha256_hex(data)
    existing = AgentDefinitionBlob.objects.filter(sha256=sha).first()
    if existing is not None:
        return existing
    storage_key, local = "", None
    if attachment_storage.is_configured():
        storage_key = _storage_key(sha)
        attachment_storage.put(storage_key, data, "application/octet-stream")
    else:
        local = data
    blob, _ = AgentDefinitionBlob.objects.get_or_create(
        sha256=sha, defaults={"size": len(data), "storage_key": storage_key, "local": local})
    return blob


def read_blob(sha256: str) -> bytes:
    blob = AgentDefinitionBlob.objects.get(sha256=sha256)
    data = attachment_storage.get(blob.storage_key).body if blob.storage_key else bytes(blob.local or b"")
    if policy.sha256_hex(data) != sha256:
        # Never hand out bytes that are not what the manifest names.
        raise ValueError(f"blob {sha256} does not match its content")
    return data


def read_file(revision: AgentDefinitionRevision, path: str) -> bytes | None:
    entry = (revision.tree or {}).get(path)
    return read_blob(entry["sha256"]) if entry else None


def files_of(revision: AgentDefinitionRevision) -> dict[str, tuple[bytes, bool]]:
    """{path: (content, executable)} of a whole revision."""
    return {path: (read_blob(e["sha256"]), bool(e.get("executable")))
            for path, e in sorted((revision.tree or {}).items())}


def tree_of(files: dict[str, tuple[bytes, bool]]) -> dict[str, dict]:
    """Store every file's blob and return the tree `{path: {sha256, size, executable}}`."""
    tree = {}
    for path, (data, executable) in sorted(files.items()):
        blob = put_blob(data)
        tree[path] = {"sha256": blob.sha256, "size": len(data), "executable": bool(executable)}
    return tree


def root_of(tree: dict[str, dict]) -> str:
    return policy.manifest_root(tree)


def source_of(agent: Agent) -> dict:
    """`{source, revision, root_sha256}`: where a runner gets this agent's definition.
    A repo agent's is `{"source": "repo", "revision": None, "root_sha256": None}`."""
    live = agent.live_revision if agent.is_stored else None
    return {"source": agent.source, "revision": live.number if live else None,
            "root_sha256": live.root_sha256 if live else None}


# --- the files canopy writes itself ---------------------------------------


def _description(agent: Agent) -> str:
    text = " ".join((agent.description or agent.persona or agent.name or agent.slug).split())
    return text[:300]


def generated_files(agent: Agent, number: int) -> dict[str, tuple[bytes, bool]]:
    """The server-generated files of revision `number`, rewritten on every publish.

    The plugin manifest declares no hooks and no MCP servers, whatever a tree held before:
    a stored agent runs only its skills, under canopy's own plugin."""
    version = f"0.{number}.0"
    description = _description(agent)
    plugin = {"name": agent.slug, "version": version, "description": description,
              "author": {"name": CANOPY_NAME, "email": CANOPY_EMAIL},
              "keywords": ["agent", "canopy", agent.slug]}
    marketplace = {"name": agent.slug, "description": description,
                   "owner": {"name": CANOPY_NAME, "email": CANOPY_EMAIL},
                   "metadata": {"version": version},
                   "plugins": [{"name": agent.slug, "source": "./", "version": version,
                                "description": description}]}
    settings_json = {"env": {"CANOPY_AGENT": agent.slug}}
    out = {
        ".claude-plugin/plugin.json": (_json(plugin), False),
        ".claude-plugin/marketplace.json": (_json(marketplace), False),
        ".claude/settings.json": (_json(settings_json), False),
        ".gitignore": (b".env\n__pycache__/\n*.pyc\n.DS_Store\n", False),
        f"bin/{agent.slug}-email": (_email_shim(agent), True),
    }
    assert set(out) == policy.generated_paths(agent.slug)
    return out


def _json(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _email_shim(agent: Agent) -> bytes:
    """`bin/<slug>-email` exactly as the agent factory writes it."""
    with contextlib.redirect_stderr(io.StringIO()):  # the factory warns when there is no mailbox
        rendered = render(AgentSpec(slug=agent.slug, display_name=agent.name or agent.slug,
                                    mandate=agent.description or agent.name or agent.slug,
                                    mailbox=agent.email or "", author_name=CANOPY_NAME,
                                    author_email=CANOPY_EMAIL))
    content, _ = rendered[f"bin/{agent.slug}-email"]
    return content.encode("utf-8")


# --- the archive runners and laptops materialize --------------------------


def archive(revision: AgentDefinitionRevision) -> bytes:
    """The revision as a gzip'd tar: regular files only, mode 0644 or 0755, sorted, with
    fixed owners and times, so the same revision is the same bytes every time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, (data, executable) in files_of(revision).items():
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o755 if executable else 0o644
            info.type = tarfile.REGTYPE
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


# --- what the core reads from a revision ----------------------------------

_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---", re.S)
_SKILL_PATH = re.compile(r"^skills/([^/]+)/SKILL\.md$")


def frontmatter(text: str) -> dict[str, str]:
    """`name`, `description` (folded) and the few flags canopy reads, from a SKILL.md —
    the same reading `canopy agent-publish` makes of a repo."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    name = re.search(r"^name:\s*(.+)$", block, re.M)
    if name:
        out["name"] = name.group(1).strip().strip("'\"")
    desc = re.search(r"^description:\s*(?:[>|]-?\s*)?\n?((?:.|\n)*?)(?:\n[\w-]+:|\Z)", block, re.M)
    if desc:
        out["description"] = " ".join(line.strip() for line in desc.group(1).splitlines()).strip().strip("'\"")
    for key in ("user-invocable", "launchable", "argument-hint"):
        hit = re.search(rf"^{key}:\s*(.+)$", block, re.M)
        if hit:
            out[key] = hit.group(1).strip().strip("'\"")
    return out


def skills_of(revision: AgentDefinitionRevision) -> list[dict]:
    """The skill catalog of a revision: one row per `skills/<dir>/SKILL.md` that names itself."""
    rows = []
    for path in sorted(revision.tree or {}):
        m = _SKILL_PATH.match(path)
        if not m:
            continue
        data = read_file(revision, path) or b""
        fm = frontmatter(data.decode("utf-8", errors="replace"))
        name = (fm.get("name") or m.group(1)).strip()[:120]
        rows.append({
            "name": name,
            "description": fm.get("description", ""),
            "path": path,
            "launchable": fm.get("launchable", "").lower() == "true",
            "internal": fm.get("user-invocable", "").lower() == "false",
            "args_hint": fm.get("argument-hint", "")[:120],
        })
    return rows


def refresh_skill_catalog(agent: Agent) -> int:
    """Replace a stored agent's catalog with its LIVE revision's skills: the publish of a
    repo agent's catalog, done by canopy because it holds the files."""
    revision = agent.live_revision
    if revision is None:
        return agent.skills.count()
    rows = skills_of(revision)
    agent.skills.all().delete()
    AgentSkill.objects.bulk_create([
        AgentSkill(agent=agent, name=r["name"], description=r["description"],
                   url=scoped_url(agent.workspace, f"/agents/{agent.slug}/definition?file={r['path']}")[:500],
                   launchable=r["launchable"], internal=r["internal"], args_hint=r["args_hint"],
                   source=AgentSkill.PUBLISHED)
        for r in {r["name"]: r for r in rows}.values()
    ])
    return agent.skills.count()
