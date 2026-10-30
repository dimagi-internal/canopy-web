"""The projects connection — `/api/mcp/projects/` — every project's toolbox through
one small, fixed set of tools.

A project carries what its people and agents share (`ProjectService`): a knowledge
base, skills, shared MCP servers, plugins. Giving every project its own tools in
every session would bury the work in tools it does not need, and the count would
grow with each project. So one connection serves every project with the same few
tools, whatever exists, and a project's contents reach the session only when the
work asks for them:

1. **Find** — `projects`: one line per project the caller may open that carries
   something.
2. **Open** — `open_project("<ws>/<handle>")`: the project's card — what it is for,
   what its knowledge base holds and how to search it, its skills by name, the
   servers and plugins it shares with how to add them.
3. **Use** — the knowledge-base tools (`kb_search`, `kb_read`, … `kb_table`), each
   taking the `project` it reads; `project_skill(project, skill)` for a skill's
   instructions.

Nothing is remembered between calls: under MCP 2026-07-28 (what Claude Code speaks)
a connection has no session for a server to keep state in, so every call names its
project. That also makes it survive a restart and need no store.

canopy serves what it holds — a knowledge base, a skill's instructions. It does not
proxy a shared remote MCP server (it holds no third-party credentials) or load a
plugin (plugins load when Claude Code starts): those are on the card with the one
line that adds them.

Access is the project's: a member of its workspace or its lead agent's, and for a
knowledge base made from private Drive folders only its readers
(`projects.readable_project`), checked on every call.
"""
from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from canopy_kb.tools import ORIGINAL_TOOL, _base_tools
from django.db.models import Q
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.base import Provider
from fastmcp.tools.base import Tool, ToolResult

from .projects import _call, _current_user, off_the_shared_thread, readable_project

log = logging.getLogger(__name__)

#: A knowledge-base tool's name here: its own name with this prefix (`kb_info` keeps its own).
KB_PREFIX = "kb_"

INSTRUCTIONS = (
    "A canopy project can carry a toolbox: a knowledge base, skills, shared MCP servers. When "
    "work belongs to a project (a task's 'Project:' line names it), call open_project with its "
    "address first for its card; call projects to find one. The kb_ tools read a project's "
    "knowledge base and take its address as `project`; project_skill reads a skill when the "
    "work needs it.")


# ---- which projects, and what they carry -----------------------------------------


def _openable(user, query: str = "") -> list:
    """Projects `user` may open that carry something, by address."""
    from apps.agents.models import AgentProject  # noqa: PLC0415 — models: imported before apps load
    from apps.agents import services  # noqa: PLC0415 — models: imported before apps load
    from apps.workspaces import services as wsvc  # noqa: PLC0415 — models: imported before apps load

    if user is None:
        return []
    slugs = wsvc.user_workspace_slugs(user)
    qs = (AgentProject.objects.filter(Q(workspace_id__in=slugs) | Q(agent__workspace_id__in=slugs) | services.shared_with_q(slugs))
          .filter(served__isnull=False).exclude(status=AgentProject.ARCHIVED).distinct()
          .select_related("agent", "workspace").prefetch_related("served").order_by("workspace_id", "handle"))
    words = [w for w in (query or "").lower().split() if w]
    out = []
    for p in qs:
        hay = " ".join([p.address, p.name, p.outcome or ""] + [s.name for s in p.served.all()]).lower()
        if all(w in hay for w in words):
            out.append(p)
    return out


def _resolve(user, ref: str):
    """The project `ref` names — `<ws>/<handle>`, or a handle only one openable
    project has — if `user` may open it; else a ToolError saying what to do."""
    ref = (ref or "").strip().strip("/").lower()
    projects = _openable(user)
    hit = [p for p in projects if (p.address if "/" in ref else p.handle) == ref]
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:
        raise ToolError(f"{ref!r} names {len(hit)} projects: "
                        + ", ".join(p.address for p in hit) + " — give the workspace too")
    raise ToolError(f"no project {ref!r} that you may open carries anything — call projects to list them")


def _kb_readable(user, project) -> bool:
    return readable_project(user, project.workspace_id, project.handle) is not None


def _summary(user, project) -> str:
    """What a project carries, in a few words: `knowledge base, 2 skills`."""
    counts: dict[str, int] = {}
    for s in project.served.all():
        if s.kind == "kb" and not _kb_readable(user, project):
            continue
        counts[s.kind] = counts.get(s.kind, 0) + 1
    words = {"kb": ("knowledge base", "knowledge bases"), "skill": ("skill", "skills"),
             "mcp": ("shared MCP server", "shared MCP servers"), "plugin": ("plugin", "plugins")}
    parts = []
    for kind in ("kb", "skill", "mcp", "plugin"):
        n = counts.get(kind)
        if n:
            one, many = words[kind]
            parts.append(one if n == 1 and kind == "kb" else f"{n} {one if n == 1 else many}")
    return ", ".join(parts) or "nothing you may use"


# ---- the knowledge-base tools, one set for every project --------------------------


def _kb_name(tool: str) -> str:
    return tool if tool.startswith(KB_PREFIX) else KB_PREFIX + tool


def kb_tool_specs() -> list[dict]:
    """Every knowledge-base tool, named `kb_<tool>`, with a required `project`.
    A KB's per-table tools are not here: `kb_table` reads any table by name."""
    out = []
    for spec in _base_tools() + [ORIGINAL_TOOL]:
        schema = json.loads(json.dumps(spec.get("inputSchema") or {"type": "object", "properties": {}}))
        schema.setdefault("properties", {})["project"] = {
            "type": "string", "description": "the project's address, <workspace>/<handle> (see projects)"}
        schema["required"] = ["project"] + list(schema.get("required") or [])
        out.append({"name": _kb_name(spec["name"]), "kb_tool": spec["name"],
                    "description": f"A project's knowledge base: {spec['description']}", "inputSchema": schema})
    return out


def _kb_call(ref: str, tool: str, arguments: dict):
    user = _current_user()
    if user is None:
        raise ToolError("sign in to canopy to read a project")
    project = _resolve(user, ref)
    if not project.served.filter(kind="kb").exists() or not _kb_readable(user, project):
        raise ToolError(f"{project.address} has no knowledge base you may read — open_project shows what it has")
    return _call(project.workspace_id, project.handle, tool, arguments)


class KbTool(Tool):
    """One knowledge-base tool; the call's `project` says whose knowledge base."""

    kb_tool: str

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        ref = args.pop("project", "")
        out = await off_the_shared_thread(_kb_call)(ref, self.kb_tool, args)
        # The research tools answer in text an agent reads as-is (as a project's own server does).
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return ToolResult(content=text, structured_content={"result": out})


class KbToolsProvider(Provider):
    async def _list_tools(self):
        return [KbTool(name=s["name"], description=s["description"], parameters=s["inputSchema"],
                       kb_tool=s["kb_tool"]) for s in kb_tool_specs()]


# ---- the card ----------------------------------------------------------------------


def _card(user, project) -> str:
    """What a project carries and how to use each part."""
    from apps.agents import work  # noqa: PLC0415 — models: imported before apps load
    from apps.workspaces.links import base_url  # noqa: PLC0415 — models: imported before apps load

    lines = [f"{project.address} — {project.name}"]
    if (project.outcome or "").strip():
        lines.append(f"Done looks like: {project.outcome.strip()}")
    rows = work.services_of(project, origin=base_url())
    kb = next((r for r in rows if r["kind"] == "kb"), None)
    if kb and _kb_readable(user, project):
        info = _call(project.workspace_id, project.handle, "kb_info", {})
        lines.append(f'\nKnowledge base — read it with the kb_ tools, project="{project.address}". '
                     f"Start with kb_search; kb_table reads a table by name.\n{info}")
    elif kb:
        lines.append("\nIts knowledge base is not published yet, or not one you may read.")
    skills = [r for r in rows if r["kind"] == "skill"]
    if skills:
        lines.append("\nSkills (read one with project_skill when the work needs it):")
        lines += [f"- {r['name']}" + (f" — {r['description']}" if r["description"] else "") for r in skills]
    servers = [r for r in rows if r["kind"] == "mcp"]
    if servers:
        lines.append("\nShared MCP servers (canopy does not load these; add one if the work needs it):")
        lines += [f"- {r['name']}" + (f" — {r['description']}" if r["description"] else "")
                  + f"\n  {r['add_command']}" for r in servers]
    plugins = [r for r in rows if r["kind"] == "plugin"]
    if plugins:
        lines.append("\nPlugins (they load when Claude Code starts; install one for later sessions):")
        lines += [f"- {r['name']}" + (f" — {r['description']}" if r["description"] else "")
                  + f"\n  {r['add_command']}" for r in plugins]
    lines.append(f"\nIts page: {base_url()}{project.url}")
    return "\n".join(lines)


# ---- the tools ---------------------------------------------------------------------


def _need_user():
    user = _current_user()
    if user is None:
        raise ToolError("sign in to canopy to open projects")
    return user


def _list_projects(query: str) -> str:
    user = _need_user()
    projects = _openable(user, query)
    if not projects:
        return "No project you may open carries anything" + (f" matching {query!r}." if query else ".")
    lines = [f"{p.address} — {p.name} — {_summary(user, p)}" for p in projects]
    return "\n".join(lines) + "\n\nOpen one with open_project(\"<workspace>/<handle>\")."


def _open(ref: str) -> str:
    user = _need_user()
    return _card(user, _resolve(user, ref))


def _skill(project_ref: str, skill: str) -> str:
    user = _need_user()
    project = _resolve(user, project_ref)
    svc = project.served.filter(kind="skill", name=(skill or "").strip().lower()).first()
    if svc is None:
        names = ", ".join(project.served.filter(kind="skill").values_list("name", flat=True)) or "none"
        raise ToolError(f"{project.address} has no skill {skill!r} (its skills: {names})")
    if svc.instructions:
        return f"# {svc.name} ({project.address})\n\n{svc.instructions}"
    return (f"{svc.name} ({project.address}) has no instructions in canopy yet; they live at {svc.url}. "
            f"Read them there if you can, or ask the project's lead to publish them on the project.")


def _build() -> FastMCP:
    from .server import _verifier  # noqa: PLC0415 — would build the main server first

    server = FastMCP("canopy-projects", auth=_verifier(), instructions=INSTRUCTIONS)

    @server.tool
    async def projects(query: str = "") -> str:
        """The projects you may open that carry a toolbox (a knowledge base, skills, shared
        MCP servers, plugins), one line each with what they carry. `query` narrows by words
        in the name, address or description."""
        return await sync_to_async(_list_projects, thread_sensitive=True)(query)

    @server.tool
    async def open_project(project: str) -> str:
        """A project's card — `<workspace>/<handle>`, e.g. `connect/givewell-kb`: what its
        knowledge base holds (read it with the kb_ tools), its skills, and the servers and
        plugins it shares with how to add them. Open the project a task belongs to before
        working on it."""
        return await sync_to_async(_open, thread_sensitive=True)(project)

    @server.tool
    async def project_skill(project: str, skill: str) -> str:
        """A project's skill: its instructions, to follow for the work at hand. The
        project's card lists its skills."""
        return await sync_to_async(_skill, thread_sensitive=True)(project, skill)

    server.add_provider(KbToolsProvider())
    return server


gateway_mcp = _build()


def build_http_app():
    return gateway_mcp.http_app(path="/", transport="streamable-http")
