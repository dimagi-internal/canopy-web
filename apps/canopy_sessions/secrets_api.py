"""Router for /api/session-secrets — the AGENT half of chat secrets.

Called by `canopy secret list` / `canopy secret exec` from inside the session
driving a chat. The session proves which chat it is with the CHAT KEY canopy
issued when its runner claimed the chat's turn (`X-Canopy-Chat-Key`, see
`models.ChatKey`) — the `/key` routes. There is no route that takes a chat id:
a key reaches the secrets of the chat it was issued for and no other.

(The old `/{transcript_id}` routes — the caller had to be the chat's agent login
AND name the chat's Claude session id — were removed 2026-09-26.)

Bearer only. Anyone refused gets the same 404 as "nothing here", so the answer
never says whether a chat exists or which check failed. What comes back is the
value sealed (`zks1`) to the machine running the chat — canopy-web cannot open
it; `canopy secret exec` opens it with that machine's ECDH key.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.route_gates import gate

from . import secrets
from .schemas import SessionSecretOut, SessionSecretValueOut

router = Router(auth=session_auth, tags=["chat"])


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


@router.get("/key", response=list[SessionSecretOut],
            summary="Secrets shared with the chat this key was issued for (names only)")
@gate("runner")  # X-Canopy-Chat-Key
def list_for_key(request: HttpRequest):
    return [_out(r) for r in secrets.live_secrets(_keyed(request))]


#: What an older `canopy` CLI, which asks for a plaintext value, is told instead.
UPDATE_CANOPY = ("this secret is sealed to the machine running the chat, and this `canopy` is too old "
                 "to open it — update canopy (`canopy update`) and run the command again")


@router.get("/key/{name}", response=SessionSecretValueOut,
            summary="The SEALED value — for `canopy secret exec` in the session holding this chat's key")
@gate("runner", step_up="bearer only, a browser is 403; the value is sealed to the chat's machine")  # X-Canopy-Chat-Key
def value_for_key(request: HttpRequest, name: str, sealed: bool = False):
    """`zks1`, sealed to the device keys of the machine running the chat; the CLI
    opens it with its own ECDH key, bound to `binding_id` at `kv`. canopy-web holds
    no plaintext. Without `?sealed=true` (an older CLI, which expects plaintext) a
    409 `update_canopy` says to update."""
    session = _keyed(request)
    if not sealed:
        raise ProblemError(409, "Update canopy", type_=TYPE_CONFLICT, detail=UPDATE_CANOPY,
                           members={"error": "update_canopy"})
    row = secrets.resolve_secret(session, name, user=request.user)
    if row is None:
        raise HttpError(404, "no such secret")
    return {"name": name, "value_zk": row.value_zk, "binding_id": secrets.binding_id(session), "kv": secrets.KV}
