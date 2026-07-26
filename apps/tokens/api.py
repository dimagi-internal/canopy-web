"""Django Ninja v2 router for the /api/tokens/ surface."""
from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError

from .models import PersonalToken
from .schemas import (
    PersonalTokenCreatedOut,
    PersonalTokenCreateIn,
    PersonalTokenOut,
)

router = Router(auth=session_auth, tags=["tokens"])


def _serialize(token: PersonalToken) -> dict:
    return {
        "id": token.pk,
        "label": token.label,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
        "expires_at": token.expires_at,
    }


@router.get("/", response=list[PersonalTokenOut], summary="List my tokens")
def list_tokens(request: HttpRequest) -> list[PersonalTokenOut]:
    qs = PersonalToken.objects.filter(user=request.user).order_by("-created_at")
    return [PersonalTokenOut.model_validate(_serialize(t)) for t in qs]


@router.post("/", response={201: PersonalTokenCreatedOut}, summary="Mint a token")
def create_token(request: HttpRequest, payload: PersonalTokenCreateIn) -> Status:
    raw, token = PersonalToken.create_for_user(
        user=request.user, label=payload.label, ttl_days=payload.ttl_days
    )
    body = _serialize(token) | {"raw": raw}
    return Status(201, PersonalTokenCreatedOut.model_validate(body))


@router.delete("/{pk}/", response={204: None}, summary="Revoke a token (mine only)")
def revoke_token(request: HttpRequest, pk: int) -> Status:
    token = PersonalToken.objects.filter(pk=pk, user=request.user).first()
    if token is None:
        raise ProblemError(404, "Token not found", type_=TYPE_NOT_FOUND)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return Status(204, None)
