"""The browser plumbing behind a rendered MCP Apps View — for canopy USERS.

`/api/canopy-sessions/{id}/apps/{tool_call_id}/…` (spec 2026-10-08 §5). A
contact's twin lives under `/api/contact/` (`apps/contacts/contact_api.py`). The
rule is `apps/mcp_apps_host/mcp_apps_views.py`; these only resolve the session and the
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
from apps.api.route_gates import gate
from apps.harness import acting_person, sensitivity
from apps.mcp_apps_host import mcp_apps_views as views
from apps.tokens import kinds

from . import services

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
    # A chat surface (`sensitivity.request_is_chat_surface`): a browser or a site's
    # widget token — never a session cookie minted from a token
    # (`/api/debug/mint-session/`), which reads as a browser to everything else.
    if (getattr(request, "via_mcp", False) or getattr(request, "auth_method", "") == kinds.PAT
            or not sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, "app view routes are for a person's browser, not a token")


def _session(request, session_id):
    from .api import _session_or_404

    browser_only(request)
    return _session_or_404(request, session_id)


def _viewer(request) -> views.Viewer:
    return views.Viewer(user=request.user,
                        chat_surface=sensitivity.request_is_chat_surface(request),
                        person=acting_person.is_person(request))


def _run(fn, *args):
    try:
        return fn(*args)
    except views.ViewRefusal as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}") from None


@router.get("/{session_id}/apps/{tool_call_id}/resource", response=dict,
            summary="Load an app view, as the person looking at it")
# MCP Apps View plumbing (apps/mcp_apps_host/mcp_apps_views.py): a session reader may
# load a View read-only. call/context/message need a WRITE role in the session and the
# viewer's OWN host grant; read/subscribe need the viewer's own grant and `may_read` (any
# session reader of an ordinary conversation; a sensitive one's people on its chat
# surfaces only). A PAT is refused.
@gate("session-acl")
def app_view_resource(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str):
    session = _session(request, session_id)
    return _run(views.resource, session, _viewer(request), tool_call_id)


@router.post("/{session_id}/apps/{tool_call_id}/call", response=dict,
             summary="An app view's tools/call, as the person who clicked")
@gate("session-acl", kind="conversation")  # write + the viewer's own host grant
def app_view_call(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                  payload: AppCallIn):
    session = _session(request, session_id)
    return _run(views.call, session, _viewer(request), tool_call_id, payload.name,
                payload.arguments or {})


@router.post("/{session_id}/apps/{tool_call_id}/read", response=dict,
             summary="An app view's resources/read, same server only")
@gate("session-acl", kind="conversation")  # the viewer's own host grant
def app_view_read(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                  payload: AppReadIn):
    session = _session(request, session_id)
    return _run(views.read, session, _viewer(request), tool_call_id, payload.uri)


@router.put("/{session_id}/apps/{tool_call_id}/context", response=dict,
            summary="An app view's ui/update-model-context")
@gate("session-acl", kind="conversation")  # write
def app_view_context(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                     payload: AppContextIn):
    session = _session(request, session_id)
    _run(views.update_context, session, _viewer(request), tool_call_id,
         payload.dict(exclude_none=True))
    return {}


@router.post("/{session_id}/apps/{tool_call_id}/message", response=dict,
             summary="An app view's ui/message: a message from the viewer, which starts a turn")
@gate("session-acl", kind="conversation")  # write
def app_view_message(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                     payload: AppMessageIn):
    from apps.harness import initiator as who

    from . import doors

    session = _session(request, session_id)
    ref = _run(views.check_message, session, _viewer(request), tool_call_id, payload.text)
    services.note_request_grant(session, request)
    message, turn = doors.send_from(
        "app_view", session=session, text=payload.text, user=request.user,
        client_id=payload.client_id,
        # A View's clicks arrive with the delegated token the site minted for
        # its visitor; that token's runner requirements (`sensitive_data`) bind this send
        # exactly as they bind the REST send.
        runner_requirements=getattr(request, "runner_requirements", ()),
        chat_surface=sensitivity.request_is_chat_surface(request),
        initiator=who.for_request(request, via=f"app_view:{ref['site']}"),
    )
    return {"turn_id": str(turn.id) if turn else None, "message_id": str(message.id)}


@router.post("/{session_id}/apps/{tool_call_id}/subscribe", response=dict,
             summary="An app view's resources/subscribe: hear when a host resource changes")
# canopy holds the subscription (apps/mcp_apps_host/mcp_apps_views.py `subscribe`); the
# host's signed POST /api/sites/resources-updated reaches the View as the socket frame
# `app.resource_updated` — a URI, never data.
@gate("session-acl", kind="conversation")  # the viewer's own host grant
def app_view_subscribe(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                       payload: AppReadIn):
    session = _session(request, session_id)
    return _run(views.subscribe, session, _viewer(request), tool_call_id, payload.uri)


@router.post("/{session_id}/apps/{tool_call_id}/unsubscribe", response=dict,
             summary="An app view's resources/unsubscribe")
@gate("session-acl", kind="conversation")  # idempotent; the session ACL alone
def app_view_unsubscribe(request: HttpRequest, session_id: uuid.UUID, tool_call_id: str,
                         payload: AppReadIn):
    session = _session(request, session_id)
    return _run(views.unsubscribe, session, _viewer(request), tool_call_id, payload.uri)
