"""`POST /api/agents/{slug}/aws-credentials` — one hour of a named read-only role, as the agent."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.test import Client

from apps.agents import api_aws
from apps.agents.models import Agent
from apps.events.models import Event
from apps.mcp.api_tools import EXCLUDED
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

ROLE_ARN = "arn:aws:iam::858923557655:role/labs-jj-observe-readonly"


class FakeSts:
    def __init__(self, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.fail = fail

    def assume_role(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return {"Credentials": {
            "AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret", "SessionToken": "tok",
            "Expiration": dt.datetime(2026, 10, 29, 18, 0, tzinfo=dt.timezone.utc)}}


@pytest.fixture()
def sts(monkeypatch, settings):
    settings.CANOPY_AWS_ROLES = {"labs-observe": ROLE_ARN}
    fake = FakeSts()
    monkeypatch.setattr(api_aws, "sts_client", lambda: fake)
    return fake


@pytest.fixture()
def tenancy():
    ws = a_workspace("dimagi")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    viewer = a_member(ws, email="vi@dimagi.com", role=M.VIEWER)
    outsider = a_member(a_workspace("elsewhere"), email="out@example.com", role=M.OWNER)
    # The agent's own login is only a viewer: the route must accept it as itself.
    bot = a_member(ws, email="hal@dimagi-ai.com", role=M.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner, user=bot)
    return {"owner": owner, "viewer": viewer, "outsider": outsider, "bot": bot, "agent": agent}


def _pat(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _post(c, role="labs-observe", slug="hal"):
    return c.post(f"/api/agents/{slug}/aws-credentials", data=json.dumps({"role": role}),
                  content_type="application/json")


def test_agent_own_login_gets_credential_process_json_stamped_with_the_agent(tenancy, sts):
    r = _post(_pat(tenancy["bot"]))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["Version"] == 1
    assert body["AccessKeyId"] == "ASIAEXAMPLE" and body["SessionToken"] == "tok"
    assert body["Expiration"] == "2026-10-29T18:00:00Z"
    assert body["RoleArn"] == ROLE_ARN
    (call,) = sts.calls
    assert call["RoleArn"] == ROLE_ARN
    assert call["SourceIdentity"] == "canopy-agent-hal"
    assert call["DurationSeconds"] == 3600
    assert {"Key": "agent", "Value": "hal"} in call["Tags"]
    assert {"Key": "caller", "Value": "agent-login"} in call["Tags"]
    ev = Event.objects.get(kind="agent.aws.issued")
    assert ev.payload["agent"] == "hal" and ev.payload["role"] == "labs-observe"
    assert "secret" not in json.dumps(ev.payload) and "tok" not in json.dumps(ev.payload)


def test_an_editor_token_may_ask_too(tenancy, sts):
    assert _post(_pat(tenancy["owner"])).status_code == 200
    assert {"Key": "caller", "Value": f"user-{tenancy['owner'].pk}"} in sts.calls[0]["Tags"]


def test_a_viewer_is_refused_and_an_outsider_sees_no_agent(tenancy, sts):
    assert _post(_pat(tenancy["viewer"])).status_code == 403
    assert _post(_pat(tenancy["outsider"])).status_code == 404
    assert sts.calls == []


def test_a_browser_session_is_refused(tenancy, sts):
    c = Client()
    c.force_login(tenancy["owner"])
    assert _post(c).status_code == 403
    assert sts.calls == []


def test_only_roles_canopy_vends(tenancy, sts):
    r = _post(_pat(tenancy["bot"]), role="labs-admin")
    assert r.status_code == 404
    assert "labs-observe" in r.json()["detail"]
    assert sts.calls == []


def test_aws_refusal_is_a_502_with_the_reason(tenancy, monkeypatch, settings):
    settings.CANOPY_AWS_ROLES = {"labs-observe": ROLE_ARN}
    monkeypatch.setattr(api_aws, "sts_client", lambda: FakeSts(fail=RuntimeError("AccessDenied")))
    r = _post(_pat(tenancy["bot"]))
    assert r.status_code == 502
    assert "AccessDenied" in r.json()["detail"]


def test_never_an_mcp_tool():
    assert "agent_aws_credentials" in EXCLUDED
