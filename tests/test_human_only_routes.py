"""Owner actions a token cannot take for its user.

A personal access token — or an MCP client signed in for an hour, and every
REST route is an MCP tool — acts with its user's WHOLE role. So an agent session
on its owner's laptop holding the owner's PAT could publish the interface that
decides what outsiders may make the agent do, rewrite its credentials, mint more
administrators, remove people, or mint a never-expiring token that survives the
OAuth grant it was minted from being revoked. `@human_only`
(`apps/common/human_only.py`) refuses those to anything but a person in the
canopy web app.

Two halves, both pinned here:

- every such route refuses a token (a PAT, an MCP call, a session minted from a
  token) and still works from a browser;
- every route that refuses machines — through the decorator or the older inline
  `is_machine(request)` check — is left off the MCP tool list, with its reason.
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.api.api import api
from apps.common.human_only import HUMAN_ONLY
from apps.harness.models import Runner
from apps.mcp import api_tools
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

M = WorkspaceMembership
WS = "human-ws"


@pytest.fixture
def world():
    ws = a_workspace(WS)
    owner = a_member(ws, email="human-owner@dimagi.com", role=M.OWNER)
    other = a_member(ws, email="human-other@dimagi.com", role=M.EDITOR)
    Agent.objects.create(slug="humanbot", name="Human", workspace=ws, owner=owner)
    runner = Runner.objects.create(
        name="human-box", kind=Runner.EMDASH, paired_by=owner, status=Runner.ONLINE,
        workspace_id=WS, last_heartbeat_at=timezone.now(), capabilities={},
    )
    return {"ws": ws, "owner": owner, "other": other, "runner": runner}


def _routes(world):
    """(method, path, body) for every human-only route, shaped to pass schema
    validation so the refusal — not a 422 — is what answers."""
    rid, oid = world["runner"].id, world["other"].pk
    return [
        ("put", f"/api/workspaces/{WS}/parent", {"parent": None}),
        ("delete", f"/api/workspaces/{WS}/", None),
        ("delete", f"/api/workspaces/{WS}/members/{oid}/", None),
        ("patch", f"/api/workspaces/{WS}/members/{oid}/", {"role": "viewer"}),
        ("post", f"/api/workspaces/{WS}/invites/", {"email": "x@dimagi.com", "role": "owner"}),
        ("post", f"/api/workspaces/{WS}/invites/999/reissue", None),
        ("put", f"/api/workspaces/{WS}/shared-vault", {"vault": "V", "service_key": "ops_x"}),
        ("put", "/api/agents/humanbot/interface", {"source": "full: [contact]"}),
        ("delete", "/api/agents/humanbot/interface", None),
        ("put", "/api/agents/humanbot/credentials", {"values": {"k": "v"}}),
        ("put", "/api/agents/humanbot/vault", {"vault": "V", "service_key": "ops_x"}),
        ("delete", "/api/agents/humanbot/credentials/k", None),
        ("post", f"/api/harness/runners/{rid}/admins", {"email": "human-other@dimagi.com"}),
        ("delete", f"/api/harness/runners/{rid}/admins/{oid}", None),
        ("put", f"/api/slack-config/{WS}/config-token", {"refresh_token": "xoxe-1"}),
        ("delete", f"/api/slack-config/{WS}/config-token", None),
        ("post", f"/api/slack-config/{WS}/declare-agent", None),
        ("post", "/api/tokens/", {"label": "forever"}),
        # Converted from inline `is_machine` refusals so the registry, the MCP
        # exclusions and apps/api/route_gates.py all know about them.
        ("put", "/api/agents/humanbot/owner", {"user_id": None}),
        ("put", f"/api/agents/humanbot/admins/{oid}", None),
        ("delete", f"/api/agents/humanbot/admins/{oid}", None),
        ("put", "/api/agents/humanbot/canopy-user", {"user_id": None}),
        ("put", f"/api/harness/runners/{rid}/flags", {"flags": []}),
    ]


def _send(client, method, path, body, **extra):
    kwargs = {"content_type": "application/json", **extra}
    if body is not None:
        kwargs["data"] = body
    return getattr(client, method)(path, **kwargs)


def test_every_human_only_route_is_exercised_here(world):
    """A new `@human_only` route must be added to `_routes` — otherwise its
    refusal is untested."""
    assert len(_routes(world)) == len(HUMAN_ONLY)


def test_a_pat_is_refused_on_every_human_only_route(world):
    raw, _ = PersonalToken.create_for_user(user=world["owner"], label="t")
    before_tokens = PersonalToken.objects.count()
    for method, path, body in _routes(world):
        res = _send(Client(), method, path, body, HTTP_AUTHORIZATION=f"Bearer {raw}")
        assert res.status_code == 403, (method, path, res.status_code, res.content)
        assert "canopy web app" in res.json()["detail"], path
    # Nothing moved.
    assert M.objects.filter(workspace_id=WS).count() == 2
    assert not world["ws"].invites.exists()
    agent = Agent.objects.get(slug="humanbot")
    assert agent.interface == {} and agent.op_vault == ""
    assert PersonalToken.objects.count() == before_tokens


def test_a_session_minted_from_a_token_is_refused(world):
    raw, _ = PersonalToken.create_for_user(user=world["owner"], label="t")
    c = Client()
    res = c.post("/api/debug/mint-session/", HTTP_AUTHORIZATION=f"Bearer {raw}",
                 content_type="application/json")
    assert res.status_code == 200, res.content
    body = res.json()
    c.cookies[body["cookie_name"]] = body["cookie_value"]
    res = c.post("/api/tokens/", data={"label": "forever"}, content_type="application/json")
    assert res.status_code == 403


def test_the_browser_still_does_it(world):
    c = Client()
    c.force_login(world["owner"])
    res = c.post(f"/api/workspaces/{WS}/invites/", data={"email": "x@dimagi.com"},
                 content_type="application/json")
    assert res.status_code == 201, res.content
    res = c.put("/api/agents/humanbot/credentials", data={"values": {"k": "v"}},
                content_type="application/json")
    assert res.status_code == 200, res.content
    res = c.post("/api/tokens/", data={"label": "mine"}, content_type="application/json")
    assert res.status_code == 201, res.content
    res = c.patch(f"/api/workspaces/{WS}/members/{world['other'].pk}/", data={"role": "viewer"},
                  content_type="application/json")
    assert res.status_code == 200, res.content


def test_an_anonymous_caller_still_gets_401_not_the_refusal(world, settings):
    settings.REQUIRE_AUTH = False  # the login middleware is not the gate under test
    res = Client().post("/api/tokens/", data={"label": "x"}, content_type="application/json")
    assert res.status_code == 401


def test_the_refusal_does_not_depend_on_what_exists(world):
    """It runs before any lookup, so a token cannot use it to probe: an agent
    that does not exist answers exactly like one that does."""
    raw, _ = PersonalToken.create_for_user(user=world["owner"], label="t")
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    real = _send(Client(), "put", "/api/agents/humanbot/vault", {"vault": "V"}, **auth)
    ghost = _send(Client(), "put", f"/api/agents/ghost-{uuid.uuid4().hex[:6]}/vault",
                  {"vault": "V"}, **auth)
    assert real.status_code == ghost.status_code == 403
    assert real.json()["detail"] == ghost.json()["detail"]


# --- the MCP half ---------------------------------------------------------------

def _operations():
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            for op in path_view.operations:
                yield op


def _refuses_machines(view) -> bool:
    original = inspect.unwrap(view)
    if getattr(original, "human_only", None):
        return True
    try:
        return "is_machine(request)" in inspect.getsource(original)
    except OSError:  # pragma: no cover - a view with no source cannot say
        return False


def test_every_route_that_refuses_machines_is_off_mcp():
    """A tool that always refuses is noise in every client's tool list, and the
    exclusion is where its reason is written down."""
    missing = sorted(
        api.get_openapi_operation_id(op)
        for op in _operations()
        if _refuses_machines(op.view_func)
        and api.get_openapi_operation_id(op) not in api_tools.EXCLUDED
    )
    assert not missing, f"refuse machines but are offered as MCP tools: {missing}"


def test_every_human_only_registration_is_a_mounted_route():
    mounted = {(inspect.unwrap(op.view_func).__module__, inspect.unwrap(op.view_func).__name__)
               for op in _operations()}
    assert set(HUMAN_ONLY) <= mounted, set(HUMAN_ONLY) - mounted
