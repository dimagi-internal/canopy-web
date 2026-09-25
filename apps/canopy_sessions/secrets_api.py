"""Router for /api/session-secrets — the AGENT half of chat secrets.

Called by `canopy secret list` / `canopy secret exec` from inside the session
itself, which names itself by its Claude session id (`CLAUDE_CODE_SESSION_ID`,
reported by the runner as `RunnerBinding.transcript_id`). There is no route
that takes a chat id: a session can reach the secrets of the chat it is bound
to and no other. See `secrets.py` for the whole rule.

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


def _out(row) -> dict:
    return {
        "name": row.name,
        "created_by": getattr(row.created_by, "email", None),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": secrets.expires_at(row).isoformat(),
    }


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
