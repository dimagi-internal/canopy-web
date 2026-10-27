"""Cloud-runner pairing invitations: an org (`make_org`), an approved invitation, and a box
pairing by its instance role with a signed proof. Plain helpers, shared by the
invitation and several-runners-on-one-machine suites.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client

from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.aws_proof import ROLE


def _proof(htu: str) -> str:
    """A botocore-signed, unsent sts:GetCallerIdentity, as the box builds it —
    with a signed random jti, so two proofs in one second are not a replay."""
    import base64
    import json
    import secrets
    import time

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    body = "Action=GetCallerIdentity&Version=2011-06-15"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
               "X-Canopy-Proof-Htu": htu, "X-Canopy-Proof-Iat": str(int(time.time())),
               "X-Canopy-Proof-Jti": secrets.token_hex(16)}
    req = AWSRequest(method="POST", url="https://sts.amazonaws.com/", data=body, headers=headers)
    SigV4Auth(Credentials("AKIAFAKE", "fake-secret", "fake-session-token"), "sts", "us-east-1").add_auth(req)
    obj = {"method": "POST", "url": "https://sts.amazonaws.com/", "headers": dict(req.headers.items()),
           "body": body}
    return base64.b64encode(json.dumps(obj).encode()).decode()


INVITES = "/api/harness/runner-invitations/"


def _person(username: str):
    return get_user_model().objects.create_user(username=username, email=f"{username}@dimagi.com")


def make_org() -> dict:
    """jj, owner of `connect`, signed in with a fresh passkey step-up, and a PAT.
    A plain function so other modules can build the same fixture under their own."""
    jj = _person("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    browser = Client()
    browser.force_login(jj)
    grant_step_up(browser)
    raw, _ = PersonalToken.create_for_user(user=jj, label="workbench")
    return {"user": jj, "ws": ws, "browser": browser, "pat": raw}


def _pat_client(raw: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _create(client, *, name="cloud-ec2-fork", workspace="connect", role_arn=ROLE, **extra):
    return client.post(INVITES, data={"name": name, "workspace": workspace, "role_arn": role_arn, **extra},
                       content_type="application/json")


PAIR = "/api/harness/runners/pair-by-role"


PAIR_HTU = f"http://testserver{PAIR}"


def _approved(org, **extra) -> dict:
    inv = _create(_pat_client(org["pat"]), **extra).json()
    assert org["browser"].post(f"{INVITES}{inv['id']}/approve").status_code == 200
    return inv


def _pair(*, name="cloud-ec2-fork", proof=None, **extra):
    """What the box sends: no Authorization at all, only its role's proof."""
    body = {"name": name, "kind": "cloud", "host": "ubuntu@ip-10-0-0-1",
            "capabilities": {"projects": ["canopy-web"], "sessions": True, "profiles": ["x"]},
            "proof": proof if proof is not None else _proof(PAIR_HTU), **extra}
    return Client().post(PAIR, data=body, content_type="application/json")
