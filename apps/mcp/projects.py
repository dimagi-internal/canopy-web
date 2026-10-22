"""A workspace project's OWN MCP server — `/api/mcp/projects/<workspace>/<handle>/`.

canopy's main MCP server (`server.py`) is canopy the product: hundreds of tools.
Someone who only wants the GiveWell knowledge base should not install that. So a
workspace project that serves something (`AgentProject.mcp_kind`) gets a server of
its own, at its own URL, carrying only its tools:

    claude mcp add --transport http givewell-kb https://<canopy>/api/mcp/projects/connect/givewell-kb

Nothing is downloaded by the reader. The steward publishes the KB once
(`apps/agents/kb_store.py`); canopy answers every call from its copy.

**One FastMCP instance serves every project.** The URL names the project; the
dispatcher (`ProjectMcpDispatch`, mounted in `config/asgi.py`) records it on the
ASGI scope and hands the request to this app at its root, and the provider reads
it back off the HTTP request (`get_http_request`) — not a contextvar, because the
Streamable-HTTP session manager runs the server in its own task.

**Same sign-in as `/api/mcp/`.** The same verifier, the same personal tokens and
OAuth; and because the URL sits under `/api/mcp/`, the resource an OAuth client
signs in for (`/api/mcp/`, RFC 9728) already covers it — MCP clients accept a
protected resource that is a path prefix of the server URL.

**Who may call it:** a member of the project's workspace or of its steward's —
the same readers `GET /api/workspace-projects/<ws>/<handle>/` admits — and, for a
KB with a Drive source, only its owner, its steward and the readers its owner named
(`kb_drive.may_read`). Anyone else sees no tools and every call is refused, with the
same answer as a project that does not exist.

`whats_new` answers from the project's version log (`kb_versions.whats_new`).
"""
from __future__ import annotations

import json
import logging
import re

from asgiref.sync import sync_to_async
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.base import Provider
from fastmcp.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

#: Where the dispatcher puts `(workspace, handle)` on the ASGI scope.
SCOPE_KEY = "canopy.project"
_REF = re.compile(r"^/([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]{1,58}[a-z0-9])(/.*)?$")

INSTRUCTIONS = ("One canopy workspace project's tools — for a knowledge base, a cited library of one "
                "publisher's public material. Cite the url on every row you use.")


def _current_ref() -> tuple[str, str] | None:
    try:
        from fastmcp.server.dependencies import get_http_request

        return get_http_request().scope.get(SCOPE_KEY)
    except Exception:  # noqa: BLE001 - outside an HTTP request there is no project
        return None


def _current_user():
    from apps.mcp.page_tools import _current_user as page_user

    return page_user()


def readable_project(user, workspace: str, handle: str):
    """The project, if `user` may read it and it serves something; else None."""
    from apps.agents import kb_drive, services
    from apps.workspaces import services as wsvc

    if user is None:
        return None
    project = services.get_workspace_project(workspace, handle)
    if project is None or not project.mcp_path:
        return None
    if not ({project.owner_workspace_id, project.agent.workspace_id} & wsvc.user_workspace_slugs(user)):
        return None
    if not kb_drive.may_read(user, project):  # a KB made from private Drive folders: its readers only
        return None
    return project


def _specs(workspace: str, handle: str) -> list[dict]:
    from apps.agents import kb_store
    from canopy_kb.tools import tool_specs

    project = readable_project(_current_user(), workspace, handle)
    if project is None:
        return []
    kb = kb_store.open_kb(project)
    try:
        return tool_specs(kb)
    finally:
        kb.db.close()


def _call(workspace: str, handle: str, name: str, arguments: dict):
    from apps.agents import kb_rerank, kb_store, kb_vectors, kb_versions
    from canopy_kb import KBError
    from canopy_kb.tools import run

    project = readable_project(_current_user(), workspace, handle)
    if project is None:
        raise ToolError(f"no workspace project {workspace}/{handle} serving tools")
    kb = kb_store.open_kb(project)
    try:
        # `search` reranks its word and card matches when a reranker is set. Vectors
        # are dormant: `semantic_for` is None while KB_VECTORS is off (kb_vectors.py).
        rerank = kb_rerank.reranker() if name == "search" else None
        semantic = kb_vectors.semantic_for(project) if name == "search" else None
        link = kb_store.original_link(project) if name == "original" else None
        if name == "whats_new":  # the version log, not the file's own history
            return kb_versions.whats_new(project, kb, str(arguments.get("since") or ""),
                                         int(arguments.get("limit") or 100))
        return run(kb, name, arguments, semantic, rerank, link)
    except (KBError, KeyError, ValueError, TypeError) as exc:
        raise ToolError(f"{name}: {exc}") from exc
    finally:
        kb.db.close()


class ProjectTool(Tool):
    """One of a project's tools. Carries its project so a call re-checks access
    against that project, not whatever the name might mean by then."""

    workspace: str
    handle: str

    async def run(self, arguments: dict) -> ToolResult:
        out = await sync_to_async(_call, thread_sensitive=True)(
            self.workspace, self.handle, self.name, arguments or {})
        # The research tools answer in text an agent reads as-is; JSON-quoting it would
        # turn every newline into "\n" and cost the reader tokens for nothing.
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return ToolResult(content=text, structured_content={"result": out})


class ProjectToolsProvider(Provider):
    """The tools of the project named in the request's URL, for a caller who may read it."""

    async def _list_tools(self):
        ref = _current_ref()
        if ref is None:
            return []
        workspace, handle = ref
        specs = await sync_to_async(_specs, thread_sensitive=True)(workspace, handle)
        return [ProjectTool(name=s["name"], description=s.get("description") or s["name"],
                            parameters=s.get("inputSchema") or {"type": "object", "properties": {}},
                            workspace=workspace, handle=handle) for s in specs]


def _build() -> FastMCP:
    from .server import _verifier

    server = FastMCP("canopy-project", auth=_verifier(), instructions=INSTRUCTIONS)
    server.add_provider(ProjectToolsProvider())
    return server


project_mcp = _build()


def build_http_app():
    return project_mcp.http_app(path="/", transport="streamable-http")


class ProjectMcpDispatch:
    """`/api/mcp/projects/<workspace>/<handle>[/…]` → the project server at its root.

    Mounted at `/api/mcp/projects` ahead of `/api/mcp`. The project is put on the
    scope (`SCOPE_KEY`), and the request is re-rooted so the FastMCP app — built
    with `path="/"` — sees its own endpoint, with or without the trailing slash
    Claude Code drops. Anything that does not name a project is a 404."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        root = scope.get("root_path", "")
        path = scope.get("path", "")
        rest = path[len(root):] if root and path.startswith(root) else path
        m = _REF.match(rest)
        if not m:
            from starlette.responses import PlainTextResponse

            await PlainTextResponse("name a project: /api/mcp/projects/<workspace>/<handle>/",
                                    status_code=404)(scope, receive, send)
            return
        workspace, handle, tail = m.group(1), m.group(2), m.group(3) or "/"
        new_root = f"{root}/{workspace}/{handle}"
        child = {**scope, SCOPE_KEY: (workspace, handle), "root_path": new_root,
                 "path": new_root + tail, "raw_path": (new_root + tail).encode()}
        await self.app(child, receive, send)
