"""/api/hcp-admin/clients — the HCP service's client registry (apps/contacts/hcp_oauth.py).

Which applications canopy does not operate may ask people for access to their HCP
instance. Superusers only: registering a client puts its name in front of every
person it asks, so it is a fleet-wide decision. Never on MCP — an agent registering
an app that then asks people for their context is exactly the escalation to rule
out. The webhook secret is returned ONCE, at registration (or rotation).
"""
from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from pydantic import Field

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.common.schemas import StrictModel

from . import hcp_oauth
from .models import HcpClient

router = Router(auth=session_auth, tags=["hcp-admin"])


class HcpClientOut(StrictModel):
    client_id: str
    name: str
    operator: str
    description: str = ""
    redirect_uris: list[str]
    allowed_scopes: list[str]
    first_party: bool
    webhook_url: str = ""
    created_at: str
    disabled_at: str | None = None
    active_grants: int = Field(description="Grants people have given it that are still active.")


class HcpClientCreatedOut(HcpClientOut):
    webhook_secret: str | None = Field(
        default=None, description="The HMAC key for revocation notifications (HCP 4.2.3). "
                                  "Shown once; store it with the client.")


class HcpClientIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    operator: str = Field(min_length=1, max_length=120, description="Who runs it, as people should read it.")
    description: str = Field(default="", max_length=500)
    redirect_uris: list[str] = Field(min_length=1)
    allowed_scopes: list[str] = Field(min_length=1, description="hcp:{category}:{read|write}, each named.")
    first_party: bool = False
    webhook_url: str = ""


class HcpClientPatch(StrictModel):
    name: str | None = Field(default=None, max_length=120)
    operator: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    redirect_uris: list[str] | None = None
    allowed_scopes: list[str] | None = None
    first_party: bool | None = None
    webhook_url: str | None = None
    disabled: bool | None = Field(default=None, description="true stops every token it holds at once.")
    rotate_webhook_secret: bool = False


def _require_superuser(request: HttpRequest) -> None:
    if not getattr(request.user, "is_superuser", False):
        raise ProblemError(403, "Only canopy administrators manage HCP clients", type_=TYPE_FORBIDDEN)


def _bad(detail: str) -> ProblemError:
    return ProblemError(422, "Invalid client", type_=TYPE_VALIDATION, detail=detail)


def _out(c: HcpClient) -> dict:
    return {"client_id": c.client_id, "name": c.name, "operator": c.operator,
            "description": c.description, "redirect_uris": list(c.redirect_uris or []),
            "allowed_scopes": list(c.allowed_scopes or []), "first_party": c.first_party,
            "webhook_url": c.webhook_url, "created_at": c.created_at.isoformat(),
            "disabled_at": c.disabled_at.isoformat() if c.disabled_at else None,
            "active_grants": c.grants.filter(status="active").count()}


def _clean(redirect_uris, scopes, webhook_url):
    try:
        uris = [hcp_oauth.check_redirect_uri(u) for u in (redirect_uris or [])]
        scopes = hcp_oauth.check_scopes(scopes or [])
        hook = (webhook_url or "").strip()
        if hook and not hook.startswith("https://"):
            raise ValueError("the webhook URL must be https")
    except ValueError as e:
        raise _bad(str(e)) from None
    return uris, scopes, hook


@router.get("/clients", response=list[HcpClientOut], operation_id="hcp_listClients",
            summary="HCP service: registered clients")
def hcp_list_clients(request: HttpRequest):
    """Every application registered to ask people for HCP access. Superusers only."""
    _require_superuser(request)
    return [_out(c) for c in HcpClient.objects.all()]


@router.post("/clients", response={201: HcpClientCreatedOut}, operation_id="hcp_registerClient",
             summary="HCP service: register a client")
def hcp_register_client(request: HttpRequest, payload: HcpClientIn):
    """Register an application. Returns its client_id, and its webhook secret
    once if a webhook URL is given. Superusers only."""
    _require_superuser(request)
    uris, scopes, hook = _clean(payload.redirect_uris, payload.allowed_scopes, payload.webhook_url)
    secret, encrypted = hcp_oauth.new_webhook_secret() if hook else (None, "")
    c = HcpClient.objects.create(
        client_id=hcp_oauth.new_client_id(), name=payload.name.strip(),
        operator=payload.operator.strip(), description=payload.description.strip(),
        redirect_uris=uris, allowed_scopes=scopes, first_party=payload.first_party,
        webhook_url=hook, webhook_secret_encrypted=encrypted, registered_by=request.user)
    return 201, {**_out(c), "webhook_secret": secret}


@router.patch("/clients/{client_id}", response=HcpClientCreatedOut, operation_id="hcp_updateClient",
              summary="HCP service: change or disable a client")
def hcp_update_client(request: HttpRequest, client_id: str, payload: HcpClientPatch):
    """Change a client's registration, disable or re-enable it, or rotate its
    webhook secret (returned once). Narrowing allowed_scopes does not change grants
    people already gave; disabling stops every token at once. Superusers only."""
    _require_superuser(request)
    c = HcpClient.objects.filter(client_id=client_id).first()
    if c is None:
        raise ProblemError(404, "No such client", type_=TYPE_NOT_FOUND)
    uris, scopes, hook = _clean(
        payload.redirect_uris if payload.redirect_uris is not None else c.redirect_uris,
        payload.allowed_scopes if payload.allowed_scopes is not None else c.allowed_scopes,
        payload.webhook_url if payload.webhook_url is not None else c.webhook_url)
    for field in ("name", "operator", "description"):
        value = getattr(payload, field)
        if value is not None:
            setattr(c, field, value.strip())
    if payload.first_party is not None:
        c.first_party = payload.first_party
    c.redirect_uris, c.allowed_scopes, c.webhook_url = uris, scopes, hook
    secret = None
    if hook and (payload.rotate_webhook_secret or not c.webhook_secret_encrypted):
        secret, c.webhook_secret_encrypted = hcp_oauth.new_webhook_secret()
    if not hook:
        c.webhook_secret_encrypted = ""
    if payload.disabled is not None:
        c.disabled_at = timezone.now() if payload.disabled else None
    c.save()
    return {**_out(c), "webhook_secret": secret}
