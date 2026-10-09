"""The browser plumbing behind a rendered MCP Apps View — for canopy USERS.

`/api/canopy-sessions/{id}/apps/{tool_call_id}/…` (spec 2026-10-08 §5). A
contact's twin lives under `/api/contact/` (`apps/tokens/contact_api.py`). The
rule is `apps/tokens/mcp_apps_views.py`; these only resolve the session and the
person.

**Never the agent's.** Every route here is excluded from MCP (`api_tools.EXCLUDED`,
`_BROWSER`) and refuses a personal access token, which is what an agent holds: a
View's app-only calls must never be reachable by the model, or `visibility:
["app"]` means nothing. A browser session, or the delegated token an embedding
site minted for its visitor, is what a View's clicks arrive with.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.tokens import mcp_apps_views as views

router = Router(auth=session_auth, tags=["chat"])


class AppCallIn(Schema):
    name: str
    arguments: dict | None = None


class AppReadIn(Schema):
    uri: str


class AppContextIn(Schema):
    content: list[dict] | None = None
    structuredContent: dict | None = None  # noqa: N815 - the MCP Apps wire name


class AppMessageIn(Schema):
    text: str
    client_id: str = ""


def browser_only(request: HttpRequest) -> None:
    """A View's routes answer a person's browser, never a program holding a PAT."""
    if getattr(request, "via_mcp", False) or getattr(request, "auth_method", "") == "pat":
        raise HttpError(403, "app view routes are for a person's browser, not a token")


def _session(request, session_id):
    from .api import _session_or_404

    browser_only(request)
    return _session_or_404(request, session_id)


def _viewer(request) -> views.Viewer:
    return views.Viewer(user=request.user)


def _run(fn, *args):
    try:
        return fn(*args)
    except views.ViewRefusal as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}") from None


@router.get("/{session_id}/apps/{tool_call_id}/resource", response=dict,
            summary="Load an app view, as the person looking at it")
def app_view_resource(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str):
    session = _session(request, session_id)
    return _run(views.resource, session, _viewer(request), tool_call_id)


@router.post("/{session_id}/apps/{tool_call_id}/call", response=dict,
             summary="An app view's tools/call, as the person who clicked")
def app_view_call(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                  payload: AppCallIn):
    session = _session(request, session_id)
    return _run(views.call, session, _viewer(request), tool_call_id, payload.name,
                payload.arguments or {})


@router.post("/{session_id}/apps/{tool_call_id}/read", response=dict,
             summary="An app view's resources/read, same server only")
def app_view_read(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                  payload: AppReadIn):
    session = _session(request, session_id)
    return _run(views.read, session, _viewer(request), tool_call_id, payload.uri)


@router.put("/{session_id}/apps/{tool_call_id}/context", response=dict,
            summary="An app view's ui/update-model-context")
def app_view_context(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                     payload: AppContextIn):
    session = _session(request, session_id)
    _run(views.update_context, session, _viewer(request), tool_call_id,
         payload.dict(exclude_none=True))
    return {}


@router.post("/{session_id}/apps/{tool_call_id}/message", response=dict,
             summary="An app view's ui/message: a message from the viewer, which starts a turn")
def app_view_message(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                     payload: AppMessageIn):
    from apps.harness import initiator as who

    from . import services

    session = _session(request, session_id)
    ref = _run(views.check_message, session, _viewer(request), tool_call_id, payload.text)
    try:
        message, turn = services.send_message(
            session=session, text=payload.text, user=request.user,
            client_id=(payload.client_id or "")[:100],
            initiator=who.for_request(request, via=f"app_view:{ref['site']}"),
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    services.maybe_execute_inline(turn)
    return {"turn_id": str(turn.id) if turn else None, "message_id": str(message.id)}
