"""Router for /api/session-secrets — the AGENT half of chat secrets.

Called by `canopy secret list` / `canopy secret exec` from inside the session
driving a chat. The session proves which chat it is with the CHAT KEY canopy
issued when its runner claimed the chat's turn (`X-Canopy-Chat-Key`, see
`models.ChatKey`) — the `/key` routes. There is no route that takes a chat id:
a key reaches the secrets of the chat it was issued for and no other.

The `/{transcript_id}` routes are the old way — the caller had to be the chat's
agent login AND name the chat's Claude session id — kept for one release so a
session started before its runner updated still works. Delete them next.

Bearer only. Anyone refused gets the same 404 as "nothing here", so the answer
never says whether a chat exists or which check failed.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import secrets
from .schemas import SessionSecretOut, SessionSecretValueOut

router = Router(auth=session_auth, tags=["chat"])


def _session(request: HttpRequest, transcript_id: str):
    if not request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer "):
        raise HttpError(403, "chat secrets are given only to a bearer token, never a browser session")
    session = secrets.session_for_caller(request.user, transcript_id)
    if session is None:
        raise HttpError(404, "no chat is bound to this session for you")
    return session


def _keyed(request: HttpRequest):
    if not request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer "):
        raise HttpError(403, "chat secrets are given only to a bearer token, never a browser session")
    from . import chat_keys

    session = chat_keys.from_request(request)
    if session is None:
        raise HttpError(404, "no chat for this key")
    return session


def _out(row) -> dict:
    return {
        "name": row.name,
        "created_by": getattr(row.created_by, "email", None),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": secrets.expires_at(row).isoformat(),
    }


# Registered BEFORE `/{transcript_id}`, which would otherwise swallow "key".
@router.get("/key", response=list[SessionSecretOut],
            summary="Secrets shared with the chat this key was issued for (names only)")
def list_for_key(request: HttpRequest):
    return [_out(r) for r in secrets.live_secrets(_keyed(request))]


@router.get("/key/{name}", response=SessionSecretValueOut,
            summary="PLAINTEXT — for `canopy secret exec` in the session holding this chat's key")
def value_for_key(request: HttpRequest, name: str):
    value = secrets.resolve_secret(_keyed(request), name, user=request.user)
    if value is None:
        raise HttpError(404, "no such secret")
    return {"name": name, "value": value}


@router.get("/{transcript_id}", response=list[SessionSecretOut],
            summary="Secrets shared with the chat this session is bound to (names only)")
def list_for_session(request: HttpRequest, transcript_id: str):
    return [_out(r) for r in secrets.live_secrets(_session(request, transcript_id))]


@router.get("/{transcript_id}/{name}", response=SessionSecretValueOut,
            summary="PLAINTEXT — for `canopy secret exec` inside the bound session only")
def value_for_session(request: HttpRequest, transcript_id: str, name: str):
    session = _session(request, transcript_id)
    value = secrets.resolve_secret(session, name, user=request.user)
    if value is None:
        raise HttpError(404, "no such secret")
    return {"name": name, "value": value}
