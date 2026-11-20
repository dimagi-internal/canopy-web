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


# --- governance: confirm or reject (3.8, 9.4.9) ---------------------------------------


class ConfirmationIn(_Body):
    disposition: str = Field(description="confirmed or rejected.")
    expectedVersion: int | None = Field(default=None, description="The version you are looking at.")


@router.post("/entries/{entry_id}/confirmation", operation_id="hcp_v2_confirmEntry",
             summary="HCP v2 governance: confirm or reject an entry")
@v2_route
@gate("self", setting=True)  # the person only: a Management Client acting for the Authorizing Principal
def hcp_v2_confirm_entry(request: HttpRequest, entry_id: str, payload: ConfirmationIn):
    """Confirm or reject the current version of one of your entries (HCP v2 3.8): the person only,
    over HTTP only. A confirmation appends a record to that version and gives it standing in a
    conflict; it changes neither the entry's declaration type nor who asserted it. A rejection
    withdraws the entry and returns anything it held to active (4.7.3)."""
    p = hcp_api._person_principal(request)
    fact = _entry(p, entry_id, payload.expectedVersion)
    fact = hcp.confirmations.confirm(fact, payload.disposition, actor=p.actor)
    return _ok(v2.to_v2_entry(fact))


# --- governance: standing rules (6.3, 9.4.14) and directives (6.4, 9.4.15) ----------------


class RuleIn(_Body):
    operators: list[str] | None = Field(default=None, description="Client operators it denies: a DNS domain "
                                        "(that host and its subdomains) or one operator identifier.")
    requires: list[str] | None = Field(default=None, description="Client claims: it denies only clients that "
                                       "do not make every one. Required when `operators` is absent.")
    categories: list[str] | None = Field(default=None, description="Categories it applies to; absent = every one.")


class DirectiveIn(_Body):
    action: str = Field(description="share, sign, transact, or an absolute URI.")
    effect: str = Field(description="deny, ask-first or permit.")
    text: str = Field(description="The directive in your own words: every client it applies to is shown it.")
    categories: list[str] | None = None
    operators: list[str] | None = None


@router.get("/rules", operation_id="hcp_v2_listRules", summary="HCP v2 governance: my standing rules")
@v2_route
@gate("self")
def hcp_v2_list_rules(request: HttpRequest):
    """Your standing rules (HCP v2 6.3): what you set once for every grant, now and later."""
    p = hcp_api._person_principal(request)
    return _ok({"rules": [hcp.rules.rule_dict(r) for r in p.person.standing_rules.all()]})


@router.post("/rules", operation_id="hcp_v2_setRule", summary="HCP v2 governance: set a standing rule")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_set_rule(request: HttpRequest, payload: RuleIn):
    """Set a deny rule (HCP v2 6.3): enforced on every grant, existing and future, from now. A grant
    it covers entirely is revoked and its client notified. The person only, over HTTP only."""
    p = hcp_api._person_principal(request)
    rule = hcp.set_standing_rule(p.person, operators=payload.operators, requires=payload.requires,
                              categories=payload.categories, actor=p.actor)
    return _ok(hcp.rules.rule_dict(rule), status=201)


@router.delete("/rules/{rule_id}", operation_id="hcp_v2_removeRule", summary="HCP v2 governance: remove a standing rule")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_remove_rule(request: HttpRequest, rule_id: str):
    """Remove a standing rule you set. The only way to override one (6.3)."""
    p = hcp_api._person_principal(request)
    hcp.rules.remove_rule(p.person, hcp.parse_entry_id(rule_id), actor=p.actor)
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


@router.get("/directives", operation_id="hcp_v2_listDirectives", summary="HCP v2 governance: my directives")
@v2_route
@gate("self")
def hcp_v2_list_directives(request: HttpRequest):
    """Your directives (HCP v2 6.4): what clients may do with what they received."""
    p = hcp_api._person_principal(request)
    return _ok({"directives": [hcp.rules.directive_dict(d) for d in p.person.directives.all()]})


@router.post("/directives", operation_id="hcp_v2_setDirective", summary="HCP v2 governance: set a directive")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_set_directive(request: HttpRequest, payload: DirectiveIn):
    """Set a directive (HCP v2 6.4). Every client it applies to receives its text with each result,
    so keep a directive about sensitive context to its categories. Directives only narrow."""
    p = hcp_api._person_principal(request)
    d = hcp.rules.set_directive(p.person, action=payload.action, effect=payload.effect, text=payload.text,
                                categories=payload.categories, operators=payload.operators, actor=p.actor)
    return _ok(hcp.rules.directive_dict(d), status=201)


@router.delete("/directives/{directive_id}", operation_id="hcp_v2_removeDirective",
               summary="HCP v2 governance: remove a directive")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_remove_directive(request: HttpRequest, directive_id: str):
    """Remove a directive you set (HCP v2 6.4)."""
    p = hcp_api._person_principal(request)
    hcp.rules.remove_directive(p.person, hcp.parse_entry_id(directive_id), actor=p.actor)
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp
