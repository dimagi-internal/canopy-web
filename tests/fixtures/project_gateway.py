"""Helpers for driving the projects connection (`/api/mcp/projects/`) the way Claude Code does:
streamable HTTP with a real personal token, through the dispatcher `config/asgi.py` mounts."""
from __future__ import annotations

import json

import httpx2
from asgiref.sync import async_to_sync
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.mcp.project_gateway import gateway_mcp
from apps.mcp.projects import ProjectMcpDispatch, project_mcp
from apps.tokens.models import PersonalToken

BASE = "https://canopy.test"
URL = "/api/mcp/projects/"


def _token(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _messages(r) -> list[dict]:
    """The JSON-RPC messages of one response, an SSE stream or a JSON body."""
    if r.headers.get("content-type", "").startswith("application/json"):
        return [r.json()]
    return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:") and line[5:].strip()]


def run_sessions(token: str, script):
    """Run `script(new_session)` against the projects connection; `new_session()`
    opens an MCP session and returns `call(method, params) -> (result, messages)`."""
    inner = gateway_mcp.http_app(path="/", transport="streamable-http")
    projects_app = project_mcp.http_app(path="/", transport="streamable-http")
    app = Starlette(routes=[Mount("/api/mcp/projects", app=ProjectMcpDispatch(projects_app, gateway=inner))])
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}

    async def main():
        async with inner.router.lifespan_context(inner):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as c:
                ids = iter(range(1, 10_000))

                async def new_session():
                    r = await c.post(URL, headers=headers, json={
                        "jsonrpc": "2.0", "id": next(ids), "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}})
                    assert r.status_code == 200, r.text
                    sid = r.headers["mcp-session-id"]
                    h = {**headers, "mcp-session-id": sid}
                    await c.post(URL, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

                    async def call(method, params=None):
                        r = await c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": next(ids),
                                                              "method": method, "params": params or {}})
                        msgs = _messages(r)
                        reply = next(m for m in msgs if "id" in m)
                        return reply.get("result") or reply.get("error"), msgs
                    return call

                return await script(new_session)
    return async_to_sync(main)()


def _text(result) -> str:
    return result["content"][0]["text"]
