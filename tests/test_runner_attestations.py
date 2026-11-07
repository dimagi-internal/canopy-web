"""Runner attestations: a host site asks who signed a runner's proof
(`apps/host_grants/runner_attestations.py`).

Driven over HTTP with requests signed the way the SDK signs them. Both proof kinds, bound
to the request they vouch for through the `htu` they sign.
"""
from __future__ import annotations

import hashlib
import time

import pytest
from canopy_sdk import sign_offs as wire
from canopy_sdk.jose import make_dpop_proof
from canopy_sdk.keys import generate_private_key, public_jwk, public_pem
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.host_grants import client_identity
from apps.host_grants import runner_attestations as service
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.aws_proof import ROLE, _proof

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("sts")]

SITE = "connect-labs"
ORIGIN = "https://labs.example.org"


def _htu(content: bytes = b"request-1") -> str:  # noqa: D401
    return f"{ORIGIN}/mcp/admin/agent-requests/attest/{hashlib.sha256(content).hexdigest()}"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner = get_user_model().objects.create_user("jo", "jo@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    login = get_user_model().objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, user=login)
    site_key = generate_private_key("EdDSA")
    app = AppCredential.create_credential(name=SITE, created_by=owner, workspace=ws)
    app.public_keys = [public_pem(site_key)]
    app.allowed_frame_origins = [ORIGIN]
    app.save()
    # The owner's own laptop serves the owner's agents (`runner_serves_agent`).
    laptop = Runner.objects.create(name="jo-laptop", kind=Runner.EMDASH, owner=owner, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    dkey = generate_private_key("ES256")
    jwk = device_keys.normalize_public_jwk(public_jwk(dkey))
    RunnerDeviceKey.objects.create(runner=laptop, kind=RunnerDeviceKey.SOFTWARE, public_jwk=jwk,
                                   jkt=device_keys.thumbprint(jwk), status=RunnerDeviceKey.ACTIVE)
    cloud = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerDeviceKey.objects.create(runner=cloud, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
                                   jkt=aws_iam_proof.role_jkt(ROLE), status=RunnerDeviceKey.ACTIVE)
    return {"app": app, "site_key": site_key, "dkey": dkey, "ace": ace, "laptop": laptop, "cloud": cloud}


def _post(w, body, *, key=None, audience=None):
    url = audience or wire.create_url(client_identity.public_base()).replace("/sign-offs", "/runner-attestations")
    token = wire.request_assertion(SITE, key or w["site_key"], url)
    return Client().post(service.PATH, data=body, content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _dpop(w, htu=None, **kw):
    return make_dpop_proof(w["dkey"], "POST", htu or _htu(), **kw)


def test_a_runner_key_proof_names_the_runner_and_the_agent_it_serves(w):
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu(), "agent": "ace"})
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["runner"] == "jo-laptop" and body["kind"] == "software"
    assert body["agent"] == "ace" and body["serves_agent"] is True


def test_a_runner_that_does_not_serve_the_agent_says_so(w):
    Agent.objects.filter(pk=w["ace"].pk).update(owner=None)
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu(), "agent": "ace"})
    assert res.status_code == 200
    assert res.json()["serves_agent"] is False


def test_an_unknown_agent_is_not_served(w):
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu(), "agent": "nobody"})
    assert res.json()["serves_agent"] is False and res.json()["agent"] == ""


def test_an_instance_role_proof_names_the_cloud_runner(w):
    res = _post(w, {"kind": "aws-iam", "proof": _proof(_htu(b"two")), "htu": _htu(b"two")})
    assert res.status_code == 200, res.content
    assert res.json()["runner"] == "cloud-ec2-2" and res.json()["kind"] == "aws-iam"


def test_a_proof_for_another_origin_is_refused(w):
    # The site states the htu it expects; a proof for an unregistered origin cannot match
    # it, and an unregistered origin cannot be stated at all.
    res = _post(w, {"kind": "dpop", "proof": _dpop(w, "https://evil.example.org/x"), "htu": _htu()})
    assert res.status_code == 401 and "proof_refused" in res.json()["detail"]
    res = _post(w, {"kind": "aws-iam", "proof": _proof("https://evil.example.org/x"), "htu": _htu()})
    assert res.status_code == 401
    res = _post(w, {"kind": "dpop", "proof": _dpop(w, "https://evil.example.org/x"),
                    "htu": "https://evil.example.org/x"})
    assert res.status_code == 400


def test_a_proof_for_another_request_on_the_same_site_is_refused(w):
    other = _dpop(w, _htu(b"some other request"))
    assert _post(w, {"kind": "dpop", "proof": other, "htu": _htu(b"this request")}).status_code == 401
    assert _post(w, {"kind": "aws-iam", "proof": _proof(_htu(b"a")), "htu": _htu(b"b")}).status_code == 401


def test_a_proof_is_single_use(w):
    proof = _dpop(w)
    assert _post(w, {"kind": "dpop", "proof": proof, "htu": _htu()}).status_code == 200
    assert _post(w, {"kind": "dpop", "proof": proof, "htu": _htu()}).status_code == 401
    aws = _proof(_htu(b"three"))
    assert _post(w, {"kind": "aws-iam", "proof": aws, "htu": _htu(b"three")}).status_code == 200
    assert _post(w, {"kind": "aws-iam", "proof": aws, "htu": _htu(b"three")}).status_code == 401


def test_a_stale_proof_is_refused(w):
    old = make_dpop_proof(w["dkey"], "POST", _htu(), now=time.time() - 600)
    assert _post(w, {"kind": "dpop", "proof": old, "htu": _htu()}).status_code == 401


def test_a_revoked_or_unknown_key_is_refused_with_one_wording(w):
    RunnerDeviceKey.objects.filter(runner=w["laptop"]).update(status=RunnerDeviceKey.REVOKED)
    revoked = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu()})
    stranger = _post(w, {"kind": "dpop", "proof": make_dpop_proof(generate_private_key("ES256"), "POST", _htu()), "htu": _htu()})
    assert revoked.status_code == stranger.status_code == 401
    assert revoked.json() == stranger.json()


def test_a_retired_runner_is_refused(w):
    Runner.objects.filter(pk=w["laptop"].pk).update(status=Runner.RETIRED)
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu(), "agent": "ace"})
    assert res.status_code == 401


def test_the_request_must_be_signed_by_the_site(w):
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu()}, key=generate_private_key("EdDSA"))
    assert res.status_code == 401
    assert Client().post(service.PATH, data={"kind": "dpop", "proof": _dpop(w), "htu": _htu()},
                         content_type="application/json").status_code == 401


def test_a_sign_off_request_is_not_an_attestation_request(w):
    res = _post(w, {"kind": "dpop", "proof": _dpop(w), "htu": _htu()}, audience=wire.create_url(client_identity.public_base()))
    assert res.status_code == 401


def test_a_bad_kind_is_malformed(w):
    assert _post(w, {"kind": "rsa", "proof": "x", "htu": _htu()}).status_code == 400
