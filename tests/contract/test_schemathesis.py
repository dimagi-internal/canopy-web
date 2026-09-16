"""Property-based contract tests: fuzz the OpenAPI spec against the running app.

Auto-generates a request for every (path × method) combination, hits the
endpoint, and asserts:
- response status matches one declared in the spec
- response body matches the declared response schema
- response content-type matches the spec

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
from hypothesis import HealthCheck, settings  # noqa: E402

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
    _schema_available = True
except Exception:
    schema = None
    _schema_available = False

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
    response = case.call(headers=headers)
    case.validate_response(response)
