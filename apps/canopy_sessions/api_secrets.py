"""Secrets shared with a chat — the browser half (share, list, forget). The agent
half, which spends them, is `secrets_api.py`."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.errors import TYPE_CONFLICT, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup

from . import secrets
from .schemas import (
    SessionSecretIn,
    SessionSecretOut,
    SessionSecretRecipientsOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _session_or_404,
    router,
)

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


# ---- Secrets shared with a chat (models.SessionSecret) -----------------------
# The BROWSER half: share, list, forget. Values never come back here; the agent
# side (`secrets_api`) spends them, and only from the session bound to this chat.

def _secret_out(row) -> dict:
    return {
        "name": row.name,
        "created_by": getattr(row.created_by, "email", None),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": secrets.expires_at(row).isoformat(),
    }


def _share_refused(exc: secrets.ShareRefusedError) -> ProblemError:
    return ProblemError(exc.status, "Chat secret", type_=TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION,
                        detail=str(exc), members={"error": exc.code})


@router.get("/{session_id}/secrets/recipients", response=SessionSecretRecipientsOut,
            summary="The public keys a secret shared with this chat is sealed to")
@gate("session-acl")  # the share gate: a writer of the chat
def secret_recipients(request: HttpRequest, session_id: uuid.UUID):
    """The device keys of the machine running this chat, which a sharer seals the
    value to before it leaves their browser. `refusal` says why nothing can be shared
    yet: the chat is on no machine, or that machine has no approved encryption key."""
    session = _session_or_404(request, session_id, write=True)
    who = secrets.recipients(session)
    return {
        "binding_id": who.binding_id,
        "kv": secrets.KV,
        "runner_id": str(who.runner.pk) if who.runner is not None else None,
        "runner_name": who.runner.name if who.runner is not None else None,
        "recipients": [{"kind": "device", "target": k.ecdh_jkt, "label": k.label or who.runner.name,
                        "public_jwk": k.ecdh_public_jwk} for k in who.keys],
        "refusal": who.refusal or None,
        "error": who.code or None,
    }


@router.post("/{session_id}/secrets", response={201: SessionSecretOut},
             summary="Share a secret with this chat, sealed to the machine running it (write-only)")
@gate("session-acl", kind="conversation", step_up=True)  # write; browser: + step-up; token allowed
def share_secret(request: HttpRequest, session_id: uuid.UUID, payload: SessionSecretIn):
    """Takes the value only sealed (`value_zk`, `zks1`) to the keys `…/secrets/recipients`
    names, and keeps that: canopy-web cannot open it. A plaintext `value` is refused
    (422 `sealed_only`). Posts NOTHING into the chat: the person refers to it by name,
    and the session finds it with `canopy secret list`. A browser needs a passkey step-up."""
    session = _session_or_404(request, session_id, write=True)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    if payload.value is not None:
        raise _share_refused(secrets.ShareRefusedError(
            422, "sealed_only", "canopy-web no longer takes a chat secret's value: seal it to the chat's "
            "machine (GET …/secrets/recipients) and send value_zk"))
    try:
        row = secrets.set_secret(session, payload.name, payload.value_zk, user=request.user)
    except secrets.ShareRefusedError as exc:
        raise _share_refused(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return 201, _secret_out(row)


@router.get("/{session_id}/secrets", response=list[SessionSecretOut],
            summary="Secrets shared with this chat (names only, never values)")
@gate("session-acl")
def list_secrets(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return [_secret_out(r) for r in secrets.live_secrets(session)]


@router.delete("/{session_id}/secrets/{name}", response={204: None},
               summary="Forget a shared secret")
@gate("session-acl", kind="conversation")  # write
def delete_secret(request: HttpRequest, session_id: uuid.UUID, name: str):
    session = _session_or_404(request, session_id, write=True)
    session.secrets.filter(name=name).delete()
    return 204, None
