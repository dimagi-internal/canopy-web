"""A minimal fake HOST APP for canopy's MCP Apps tests — the Labs contract, small.

A real FastMCP server (reached in-process over ASGI through the gateway's
`_transport_override`, the same seam `test_site_gateway` uses) that implements
the host-app half spec 2026-10-08 describes for connect-labs:

* `workflow_run_action` carries `_meta.ui.resourceUri` and is visible to both the
  model and the app. Called WITHOUT `confirm` it previews, and — when the client
  negotiated `io.modelcontextprotocol/ui` — withholds the token from the result
  (Labs item 6). Called WITH a token it "sends", and only if the token was minted
  for the SAME caller (Labs binds it to `user.pk`).
* `workflow_action_preview_view` is app-only (`visibility: ["app"]`): previews as
  the caller and returns the caller's own token in `structuredContent`.
* `ui://labs/workflow-action-preview` is the View (`text/html;profile=mcp-app`).

Every request's bearer token is recorded, so a test can say WHICH grant a call
ran under — the viewer's, not the turn's initiator's.
"""
from __future__ import annotations

import base64
import hashlib

import jwt
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context, get_http_headers

from apps.tokens import client_identity

VIEW_URI = "ui://labs/workflow-action-preview"
VIEW_HTML = ("<!doctype html><html><head><title>preview</title></head>"
             "<body><button id=send>Send</button><script>/* view */</script></body></html>")
UI = "io.modelcontextprotocol/ui"


class FakeLabs:
    def __init__(self, *, tokens: set[str], csp: dict | None = None):
        self.tokens = set(tokens)
        self.seen: list[dict] = []
        #: (tool, token) for every tool that ran — the token names the grant.
        self.calls: list[tuple[str, str]] = []
        #: Whether each tools/list / tools/call came from a client that said it
        #: renders Views.
        self.ui_negotiated: list[bool] = []
        server = FastMCP("labs")
        fake = self

        def caller() -> str:
            auth = (get_http_headers(include_all=True) or {}).get("authorization", "")
            return auth.split(" ", 1)[1] if " " in auth else ""

        def ui_client() -> bool:
            try:
                return bool(get_context().client_supports_extension(UI))
            except Exception:  # noqa: BLE001
                return False

        @server.tool(meta={"ui": {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}})
        def workflow_run_action(run_id: int, action: str, arguments: dict | None = None,
                                confirm: str | None = None) -> dict:
            tok = caller()
            fake.calls.append(("workflow_run_action", tok))
            fake.ui_negotiated.append(ui_client())
            if confirm is None:
                out = {"preview": True, "label": f"{action} for run {run_id}"}
                if not ui_client():
                    out["confirm"] = f"confirm-{tok}"
                else:
                    out["note"] = "the person confirms in the preview shown to them"
                return out
            if confirm != f"confirm-{tok}":
                raise ValueError("confirm token was not minted for you")
            return {"sent": True, "execution_id": 77}

        @server.tool(meta={"ui": {"resourceUri": VIEW_URI, "visibility": ["app"]}})
        def workflow_action_preview_view(run_id: int, action: str,
                                         arguments: dict | None = None) -> dict:
            tok = caller()
            fake.calls.append(("workflow_action_preview_view", tok))
            return {"label": action, "image": {"data_uri": "data:image/png;base64,AAAA"},
                    "confirm": f"confirm-{tok}", "confirm_expires_in": 300}

        # Model- and app-visible but read-only (MCP readOnlyHint), like Labs' status poll.
        @server.tool(annotations={"readOnlyHint": True})
        def workflow_action_status(run_id: int, execution_id: int | None = None) -> dict:
            fake.calls.append(("workflow_action_status", caller()))
            return {"executions": [{"id": execution_id or 77, "status": "completed"}]}

        @server.tool(meta={"ui": {"visibility": ["model"]}})
        def model_only_report(run_id: int) -> dict:
            fake.calls.append(("model_only_report", caller()))
            return {"report": run_id}

        @server.tool
        def marketplace_orgs_get() -> dict:
            fake.calls.append(("marketplace_orgs_get", caller()))
            return {"orgs": ["llo-foo"]}

        # The deprecated flat key alone: canopy must NOT treat this as a View.
        @server.tool(meta={"ui/resourceUri": "ui://labs/legacy"})
        def legacy_flat_meta() -> dict:
            fake.calls.append(("legacy_flat_meta", caller()))
            return {}

        @server.resource(VIEW_URI, mime_type="text/html;profile=mcp-app",
                         meta={"ui": {"prefersBorder": True,
                                      **({"csp": csp} if csp is not None else {})}})
        def view() -> str:
            fake.calls.append(("resources/read", caller()))
            return VIEW_HTML

        self.inner = server.http_app(path="/mcp/", stateless_http=True, json_response=True)

    async def app(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self.seen.append(headers)
            error = self._check(scope, headers)
            if error:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": error.encode()})
                return
        await self.inner(scope, receive, send)

    def _check(self, scope, headers) -> str:
        auth = headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme != "DPoP" or token not in self.tokens:
            return "bad authorization"
        proof = headers.get("dpop", "")
        try:
            jwk = jwt.get_unverified_header(proof)["jwk"]
            claims = jwt.decode(proof, jwt.PyJWK.from_dict(jwk).key, algorithms=["EdDSA", "ES256"])
        except Exception as exc:  # noqa: BLE001
            return f"bad proof {exc}"
        if client_identity.thumbprint(jwk) != client_identity.dpop_jkt():
            return "proof key is not the bound key"
        ath = base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).decode()
        if claims.get("ath") != ath.rstrip("="):
            return "ath"
        return ""

    def lifespan(self):
        return self.inner.router.lifespan_context(self.inner)

    def tools_called(self) -> list[str]:
        return [name for name, _ in self.calls]
