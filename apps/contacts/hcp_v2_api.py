"""/api/hcp/v2 — the Human Context Protocol v2 (draft 3) over HTTP.

v2 is canopy's internal model (`hcp/v2.py`); v1 (`hcp_api.py`) stays the wire every existing caller
uses, as an adapter over the same rows. Here are the v2 routes, planed as the spec has them (1.6):

* **governance** (the person through a Management Client — their own session or PAT, never an
  agent's login, a caller token, an OAuth client's token or a contact's), HTTP only, never MCP;
* **context** (any client holding a grant that covers the operation).

Every route is HTTP-only for now: an MCP binding for v2 is later work, so each route is listed in
`apps/mcp/api_tools.py::EXCLUDED`. Errors are RFC 9457 problems with the registered types (9.7),
and every response carries `HCP-Version: 2.0` and no `hcp_version` in the body (9.3).
"""
from __future__ import annotations

import json
from functools import wraps

from django.http import HttpRequest, HttpResponse, JsonResponse
from ninja import Router
from pydantic import Field

from apps.api.route_gates import gate

from . import hcp, hcp_api
from .hcp import v2
from .hcp.core import PROBLEM_BASE

router = Router(auth=hcp_api._HcpAuth(), tags=["hcp-v2"])
LD_JSON = "application/ld+json"


class _Body(hcp_api._Body):
    pass


def _ok(body: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(body, status=status, content_type=LD_JSON, safe=not isinstance(body, list))
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


def _problem(request: HttpRequest, err: hcp.HcpError) -> HttpResponse:
    body = {"type": PROBLEM_BASE + err.problem, "title": err.problem.replace("-", " "),
            "status": err.status, "detail": err.detail or err.problem, "instance": request.path}
    resp = HttpResponse(json.dumps(body), status=err.status, content_type="application/problem+json")
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    if err.status == 429:
        resp["Retry-After"] = "60"
    return resp


def v2_route(view):
    """The version check (9.3) and HcpError → problem+json, on every v2 route."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        try:
            asked = (request.headers.get("HCP-Version") or "").strip()
            if asked and asked != v2.HCP_VERSION_V2:
                raise hcp.HcpError("unsupported-version", f"this is the v2 binding; it serves {v2.HCP_VERSION_V2}")
            return view(request, *args, **kwargs)
        except hcp.HcpError as err:
            return _problem(request, err)
    return wrapper


def _entry(p, entry_id: str, expected_version: int | None):
    """The person's own entry, current version; a stale `expectedVersion` is a version conflict (5.4)."""
    fact = hcp.current(hcp.parse_entry_id(entry_id))
    if fact is None or fact.person_id != p.person.pk:
        raise hcp.denied()
    if expected_version is not None and fact.version != expected_version:
        raise hcp.HcpError("version-conflict", f"the entry is at version {fact.version}")
    return fact


# --- governance: an entry's purposes (4.2.6, 9.4.10) ---------------------------------


class PurposesIn(_Body):
    permittedPurposes: list[str] | None = Field(
        description="The purpose codes the entry may be used for, narrower than its writer permitted; "
                    "null removes your narrowing (never the writer's).")
    expectedVersion: int | None = Field(default=None, description="The version you are looking at.")


@router.put("/entries/{entry_id}/purposes", operation_id="hcp_v2_restrictPurposes",
            summary="HCP v2 governance: limit what an entry may be used for")
@v2_route
@gate("self", setting=True)  # the person only: a Management Client acting for the Authorizing Principal
def hcp_v2_restrict_purposes(request: HttpRequest, entry_id: str, payload: PurposesIn):
    """Set or clear the Authorizing Principal's narrowing of an entry's permitted purposes (HCP v2
    4.2.6, a governance operation: the person, never an agent, over HTTP only). It can only narrow;
    recorded as `context.restricted` naming the lists before and after."""
    p = hcp_api._person_principal(request)
    fact = _entry(p, entry_id, payload.expectedVersion)
    fact = hcp.restrict_purposes(fact, payload.permittedPurposes, actor=p.actor)
    return _ok(v2.to_v2_entry(fact))
