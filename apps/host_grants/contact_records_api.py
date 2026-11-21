"""`POST /api/sites/contacts` — a host site, the system of record for its people, keeps
canopy's copy of them current (`contact_records.py`)."""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.embedding.audit import client_ip
from apps.embedding.rate_limit import SiteAgentsRateLimitError

from . import contact_records as service
from . import site_agents

router = Router(tags=["host sites"])

#: The body IS the request: a compact JWS signed with the site's own key.
_JWT_BODY = {"requestBody": {"required": True, "content": {
    "application/jwt": {"schema": {"type": "string", "description": "a compact JWS signed by the site's key"}}}}}


class ContactRecordResultOut(Schema):
    sub: str
    #: canopy's id for the contact; null when the item was refused.
    contact_id: int | None = None
    #: `created`, `updated`, `stale` (a version no newer than canopy holds: nothing changed)
    #: or `refused` (then `reason` says why).
    status: str
    reason: str | None = None


class ContactRecordsOut(Schema):
    results: list[ContactRecordResultOut]


def _token(request: HttpRequest) -> str:
    content_type = (request.content_type or "").lower()
    if content_type != "application/jwt":
        raise HttpError(415, "unsupported_media_type: send the signed request as the body, "
                             "Content-Type: application/jwt (canopy_sdk.host.contacts)")
    try:
        token = request.body.decode("ascii").strip()
    except UnicodeDecodeError:
        raise HttpError(400, "malformed: the body is not a compact JWS") from None
    if not token:
        raise HttpError(400, "malformed: the body is empty")
    return token


def _answer(results: list[dict]) -> JsonResponse:
    # As the wire says: `contact_id` on every item (null when refused), `reason` only on a
    # refused one. Built here rather than by the schema, which would write `reason: null`.
    return JsonResponse({"results": results})


def _verified(request: HttpRequest, kind: str):
    """The budgets in the order the work happens (per client before anything is parsed, per
    site before its signature is checked), then the site and its verified claims."""
    try:
        service.check_budget(client_ip(request))
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _token(request)
    try:
        claimed = site_agents.site_for_request(token)
        service.check_budget(client_ip(request), f"{claimed.workspace_id}:{claimed.name}")
        return service.verify(token, kind)
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")


@router.post("", auth=None, response=ContactRecordsOut, openapi_extra=_JWT_BODY,
             summary="A host site writes its own record of its people, signed")
@gate("host", kind="action")  # auth=None; a request signed by the site's own registered key IS the credential
def update_contacts(request: HttpRequest):
    """The host site is the system of record for the people it brings; this keeps canopy's
    copy of them current.

    The body is a compact JWS signed by the site's own key (`Content-Type:
    application/jwt`): `typ` `canopy-contact-update+jwt`, `iss` = its host-site name,
    `aud` = this URL, at most 60 s, single-use `jti`, optional `canopy_workspace`, and
    `contacts` — 1 to 100 of `{sub, display_name?, record_uri?, profile?, version}`. Build
    and send it with `canopy_sdk.host.contacts.update_contacts`.

    A missing person is created (keyed by the site and `sub`, granted nothing). Only
    `display_name`, `record_uri` (under the site's `site.person_scheme`) and `profile` (at
    most 40 keys, 4096 bytes) are written, and only when `version` is newer than the one
    canopy holds; otherwise the item is `stale`. Each item is answered on its own:
    `{results: [{sub, contact_id, status, reason?}]}`.
    """
    app, claims = _verified(request, service.UPDATE)
    return _answer(service.apply_update(app, claims))
