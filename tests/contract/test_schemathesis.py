"""Property-based contract tests: fuzz the OpenAPI spec against the running app.

Auto-generates a request for every (path × method) combination, hits the
endpoint, and asserts:
- no request produces a server error
- response body matches the declared response schema
- response content-type matches the spec

Undeclared status codes are not checked; see _EXCLUDED_CHECKS.

Auth-protected routes need `SCHEMATHESIS_AUTH_BEARER` (a raw Personal
Access Token). Mint one with:

    uv run python manage.py create_token --email ace@dimagi-ai.com \\
        --label schemathesis --create-user

Run against a live backend:
    SCHEMATHESIS_SCHEMA_URL=http://localhost:8000/api/openapi.json \\
    SCHEMATHESIS_AUTH_BEARER=<raw-pat> \\
    pytest tests/contract/
"""
from __future__ import annotations

import os

import pytest

# Schemathesis is deliberately NOT a project dependency.
#
# It is a nightly fuzz tool that talks to a live server over HTTP and imports no
# canopy code — yet as a `dev` extra it sat in the same resolution as production
# and pinned `starlette<1`, which blocked FastMCP 4 (and therefore mcp SDK 2.2,
# and therefore resource subscriptions). Every schemathesis 4.x also requires
# pytest>=9, so keeping it in-graph meant a pytest major bump across ~2900 tests
# to satisfy a tool that never runs on a PR.
#
# A nightly test tool must not be able to pin the production MCP library. It is
# installed by contract-nightly.yml into its own environment instead, and this
# module skips wherever it is absent.
schemathesis = pytest.importorskip(
    "schemathesis",
    reason="schemathesis is installed only by the nightly contract job",
)
import requests  # noqa: E402 — a schemathesis dependency, so present wherever it is
from hypothesis import HealthCheck, settings  # noqa: E402
from schemathesis.checks import not_a_server_error  # noqa: E402
from schemathesis.specs.openapi.checks import (  # noqa: E402
    negative_data_rejection,
    positive_data_acceptance,
    status_code_conformance,
    unsupported_method,
)

# Three checks describe things this API does not promise. Left on, they fail
# almost every endpoint at the first request, so the checks below them never run:
# 482 of 488 tests failed in 33s on 2026-10-15, and no server error was visible.
# - status_code_conformance: the spec does not declare its 401/403/404/422s.
# - negative_data_rejection: a session cookie is declared as required, so a
#   request with a PAT and no cookie counts as invalid data wrongly accepted.
# - unsupported_method: auth runs before routing, so an undeclared method gets
#   401, not 405.
_EXCLUDED_CHECKS = [status_code_conformance, negative_data_rejection, unsupported_method]

# Per-endpoint exclusions, each with its reason. Only checks that cannot pass for
# a reason the spec cannot express go here; a real server bug is fixed instead.
# positive_data_acceptance ("schema-valid request rejected") is the one excluded:
_OPAQUE_CREDENTIAL = (
    "the value is a signed credential (a JWS assertion or site token). Any string "
    "fits the declared schema, but only one the right key signed is accepted, and "
    "no JSON Schema can say 'carries a valid signature'. The 400 'malformed' is the "
    "designed answer for an unreadable one."
)
_DELIVERABLE_EMAIL = (
    "`email` is validated as a deliverable address (pydantic EmailStr), which "
    "refuses special-use domains such as `.test`; JSON Schema's `format: email` "
    "admits them, and has no way to say 'deliverable'."
)
_HCP_MALFORMED = (
    "The HCP protocol defines its own `malformed-request` answer (its own body, with "
    "`hcp_version`) for a request it cannot read, so the route checks its inputs "
    "itself: a schema rule would make the framework answer instead, in canopy's "
    "shape, and Ninja cannot declare a rule it does not enforce."
)
_PER_ENDPOINT_EXCLUSIONS: dict[tuple[str, str], tuple[list, str]] = {
    ("POST", "/api/auth/contact-token"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    # The site routes read `Authorization: Bearer` as a SITE token, so the CI PAT
    # this suite sends is (correctly) not one they can read.
    ("GET", "/api/sites/agents"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("POST", "/api/sites/sign-offs"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("POST", "/api/sites/runner-attestations"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("GET", "/api/sites/sign-offs/{sign_off_id}"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("POST", "/api/sites/resources-updated"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("POST", "/api/beta-requests"): ([positive_data_acceptance], _DELIVERABLE_EMAIL),
    ("POST", "/api/workspaces/{slug}/invites/"): ([positive_data_acceptance], _DELIVERABLE_EMAIL),
    ("GET", "/api/oauth/google/callback"): ([positive_data_acceptance], _OPAQUE_CREDENTIAL),
    ("GET", "/api/hcp/v1/export"): (
        [positive_data_acceptance],
        "`include`/`exclude` are comma lists of fixed words (audit, versions / "
        "deleted). " + _HCP_MALFORMED,
    ),
    ("GET", "/api/hcp/v1/audit"): (
        [positive_data_acceptance], "`cursor` is a value the route issued. " + _HCP_MALFORMED),
    ("GET", "/api/hcp/v1/grants"): (
        [positive_data_acceptance], "`cursor` is a value the route issued. " + _HCP_MALFORMED),
    ("POST", "/api/hcp/v1/preferences/search"): (
        [positive_data_acceptance], "`purpose` is required by the HCP protocol. " + _HCP_MALFORMED),
}

# Drive is an external service the CI box has no credentials for. A route that
# needs it answers 503 `drive-not-configured`, which is the honest answer here,
# not a server bug. Only that exact problem type is let through, per response.
_DRIVE_NOT_CONFIGURED = "/problems/drive-not-configured"


def _is_documented_refusal(case, response) -> bool:
    """A 4xx the operation declares in its own responses.

    `positive_data_acceptance` judges a schema-valid request by a fixed list of
    statuses (2xx, 401, 403, 404, 409, 429), and ignores the spec. But some
    refusals of a well-formed request are the contract: a rule across fields
    or against stored state ("send cut_id with cut_scene_ids", "that label
    already exists") that JSON Schema cannot state. Such a route declares the
    status (`response={201: X, 422: Problem}`) and documents the rule in its
    description; a refusal with a status the operation does NOT declare is
    still a failure."""
    declared = {str(code) for code in case.operation.responses.status_codes}
    return 400 <= response.status_code < 500 and str(response.status_code) in declared


def _excluded_checks(case, response) -> list:
    excluded = list(_EXCLUDED_CHECKS)
    extra = _PER_ENDPOINT_EXCLUSIONS.get((case.operation.method.upper(), case.operation.path))
    if extra:
        excluded += extra[0]
    if _is_documented_refusal(case, response):
        excluded.append(positive_data_acceptance)
    if response.status_code == 503:
        try:
            problem_type = response.json().get("type", "")
        except ValueError:
            problem_type = ""
        if problem_type.endswith(_DRIVE_NOT_CONFIGURED):
            excluded.append(not_a_server_error)
    return excluded


def _own_token_ids() -> set[int]:
    """The ids of the caller's tokens, so the fuzzer cannot revoke its own credential.

    `DELETE /api/tokens/{pk}/` revokes a token of the caller's. Fuzzed with small
    integers, it revokes the very PAT the suite authenticates with, and every
    later request is a 401 — which this suite accepts, so it goes silently blind.
    """
    if not AUTH_BEARER:
        return set()
    try:
        r = requests.get(SCHEMA_URL.replace("/openapi.json", "/tokens/"),
                         headers={"Authorization": f"Bearer {AUTH_BEARER}"}, timeout=10)
        r.raise_for_status()
        return {t["id"] for t in r.json()}
    except Exception:  # noqa: BLE001 — no server: the suite skips anyway
        return set()

# Per-endpoint example count. Schemathesis is property-based: each (path × method)
# runs `max_examples` generated requests. These tests need a live server (from_uri
# below), which PR CI does not stand up — so on PRs the suite SKIPS. It runs in the
# nightly job (.github/workflows/contract-nightly.yml), which sets
# SCHEMATHESIS_MAX_EXAMPLES high for exhaustive fuzzing, and locally against a
# running server. The low default here keeps an ad-hoc local run fast.
_MAX_EXAMPLES = int(os.environ.get("SCHEMATHESIS_MAX_EXAMPLES", "6"))

SCHEMA_URL = os.environ.get(
    "SCHEMATHESIS_SCHEMA_URL", "http://localhost:8000/api/openapi.json"
)
AUTH_BEARER = os.environ.get("SCHEMATHESIS_AUTH_BEARER")

# Load schema lazily so collection passes when no server is running.
try:
    # `from_uri` in 3.x; `openapi.from_url` in 4.x. NOTE this file SKIPS in PR
    # CI (no live backend) and runs nightly, so a break here is invisible to the
    # checks on the PR that causes it — the v4 call sites were verified by
    # importing the package and inspecting signatures, not by a green run.
    schema = schemathesis.openapi.from_url(SCHEMA_URL)
    # No NUL in generated strings. The API refuses NUL in every JSON body — a value
    # or a key, at any depth — with 400 "Value cannot be stored" (apps/api/nul_guard.py,
    # tested in tests/test_contract_nightly_regressions.py), and the document says so
    # for every free-text string (`openapi_doc`). Inside free-form JSON (`dict`, `Any`)
    # no JSON Schema keyword can say it, so the fuzz would call a refusal the API
    # documents a "schema-valid request rejected". Generating no NUL is the suite
    # agreeing with that rule, not hiding a failure: the rule has its own tests.
    schema.config.generation.update(allow_x00=False)
    _schema_available = True
except Exception:
    schema = None
    _schema_available = False

_PROTECTED_TOKEN_IDS = _own_token_ids() if _schema_available else set()
#: A token id no row has, swapped in for the suite's own token's.
_ABSENT_TOKEN_ID = 2**31 - 1

# The CI user's one workspace (contract-nightly.yml makes sure there is exactly
# one), so a create resolves to it ("your sole workspace") and runs its real path
# instead of answering 422 workspace-required. The suite keeps it SOLE and intact:
_CI_WORKSPACE = os.environ.get("SCHEMATHESIS_CI_WORKSPACE", "")
_ABSENT_WORKSPACE = "contract-ci-absent"
#: Routes that could delete it, or remove or demote the CI user in it.
_GUARDS_CI_WORKSPACE = {
    ("DELETE", "/api/workspaces/{slug}/"),
    ("DELETE", "/api/workspaces/{slug}/members/{user_id}/"),
    ("PATCH", "/api/workspaces/{slug}/members/{user_id}/"),
    ("PUT", "/api/workspaces/{slug}/parent"),
}


def _protect_the_suites_own_state(case, op) -> None:
    """Steer a fuzzed request away from the credential and workspace the suite runs as."""
    params = case.path_parameters or {}
    if op == ("DELETE", "/api/tokens/{pk}/") and params.get("pk") in _PROTECTED_TOKEN_IDS:
        params["pk"] = _ABSENT_TOKEN_ID
    if _CI_WORKSPACE and op in _GUARDS_CI_WORKSPACE and params.get("slug") == _CI_WORKSPACE:
        params["slug"] = _ABSENT_WORKSPACE


def _undo_a_new_workspace(op, response, headers) -> None:
    """A fuzzed `POST /api/workspaces/` that succeeds gives the CI user a second
    workspace, and every later create would 422 as ambiguous. Delete it again."""
    if op != ("POST", "/api/workspaces/") or response.status_code != 201:
        return
    slug = response.json().get("slug")
    if slug and slug != _CI_WORKSPACE:
        requests.delete(SCHEMA_URL.replace("/openapi.json", f"/workspaces/{slug}/"),
                        headers=headers, timeout=10)

_parametrize = (
    schema.parametrize()
    if _schema_available
    else pytest.mark.skip(reason="No live backend — set SCHEMATHESIS_SCHEMA_URL")
)


@_parametrize
@settings(
    max_examples=_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_api_conforms_to_schema(case):
    headers: dict[str, str] = {}
    if AUTH_BEARER:
        headers["Authorization"] = f"Bearer {AUTH_BEARER}"
    op = (case.operation.method.upper(), case.operation.path)
    _protect_the_suites_own_state(case, op)
    response = case.call(headers=headers)
    _undo_a_new_workspace(op, response, headers)
    case.validate_response(response, excluded_checks=_excluded_checks(case, response))
