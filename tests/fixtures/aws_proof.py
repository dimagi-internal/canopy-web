"""The EC2 AWS IAM identity proof's test world: a faked STS (`sts`), botocore-signed
proofs, and a fleet of one agent on one cloud runner. Registered as a pytest plugin
(root conftest.py); `fresh_cache` clears Django's cache around a test
(`pytest.mark.usefixtures`).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import (
    Agent,
)
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.testing import give_vault_key, grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

RESOLVE = "/api/agents/eva/credentials/resolve"


HTU = f"http://testserver{RESOLVE}"


ENROLL = "/api/runner-device-keys/"


ENROLL_HTU = f"http://testserver{ENROLL}"


ROLE = "arn:aws:iam::123456789012:role/canopy-runner"


ASSUMED = "arn:aws:sts::123456789012:assumed-role/canopy-runner/i-0abc123"


BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return raw


@pytest.fixture
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


class _Res:
    def __init__(self, status, text, ctype="text/xml"):
        self.status_code, self.text, self.headers = status, text, {"Content-Type": ctype}


def _sts_xml(arn: str) -> str:
    return (
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<GetCallerIdentityResult><Arn>{arn}</Arn><UserId>AROA:i-0abc123</UserId>"
        "<Account>123456789012</Account></GetCallerIdentityResult>"
        "<ResponseMetadata><RequestId>x</RequestId></ResponseMetadata></GetCallerIdentityResponse>")


@pytest.fixture
def sts(monkeypatch):
    """Patch the one outbound call. `sts.arn` is who 'signed'; `sts.calls` what
    canopy-web sent (the URL above all)."""
    import requests

    class Fake:
        arn = ASSUMED
        status = 200
        calls: list = []

        def post(self, url, headers=None, data=None, **kw):
            self.calls.append({"url": url, "headers": dict(headers or {}), "data": data, **kw})
            if self.status != 200:
                return _Res(self.status, "<ErrorResponse/>")
            return _Res(200, _sts_xml(self.arn))

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def _proof(htu=HTU, *, iat=None, url="https://sts.amazonaws.com/", signed_extra=True,
           mutate=None) -> str:
    """A botocore-signed, unsent sts:GetCallerIdentity, as the runner builds it."""
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    if signed_extra:
        headers["X-Canopy-Proof-Htu"] = htu
        headers["X-Canopy-Proof-Iat"] = str(int(iat if iat is not None else time.time()))
    req = AWSRequest(method="POST", url=url, data=BODY, headers=headers)
    SigV4Auth(Credentials("AKIAFAKE", "fake-secret", "fake-session-token"), "sts",
              "us-east-1").add_auth(req)
    obj = {"method": "POST", "url": url, "headers": dict(req.headers.items()), "body": BODY}
    if not signed_extra:  # present, but outside the signature
        obj["headers"]["X-Canopy-Proof-Htu"] = htu
        obj["headers"]["X-Canopy-Proof-Iat"] = str(int(time.time()))
    if mutate:
        mutate(obj)
    return base64.b64encode(json.dumps(obj).encode()).decode()


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj,
                                 runtime_secrets=["CANOPY_WEB_PAT", "gog-token"])
    runner = Runner.objects.create(name="ec2-box", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    give_vault_key(jj)  # someone to seal the stored PAT to (the agent vault is zero-knowledge)
    client.force_login(jj)
    grant_step_up(client)
    client.put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": "pat-value"}},
               content_type="application/json")
    mail.outbox.clear()
    return {"client": client, "agent": agent, "runner": runner, "user": jj, "ws": ws,
            "pat": _pat(jj)}


def _enroll(fleet, *, role_arn=ROLE, proof=None, runner=None):
    return Client().post(
        ENROLL,
        data={"runner": str((runner or fleet["runner"]).pk), "kind": "aws-iam",
              "role_arn": role_arn, "proof": proof if proof is not None else _proof(ENROLL_HTU),
              "label": "ec2"},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")


def _active_role(fleet, sts):
    body = _enroll(fleet).json()
    assert fleet["client"].post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 200
    return body


