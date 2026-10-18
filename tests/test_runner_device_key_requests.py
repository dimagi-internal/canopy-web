"""A runner whose token was revoked can still ask for a device key — and only ask.

2026-10-17: a token cleanup revoked every PAT the haldimagi and jj laptop runners
held, before either had a device key. Restarted, each could neither heartbeat nor
enroll, so no approve link would ever reach their owner.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from canopy_sdk.jose import make_dpop_proof
from canopy_sdk.keys import header_jwk
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/runner-device-key-requests/"
HTU = f"http://testserver{URL}"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def box():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=jj,
                                   workspace=ws, status=Runner.ONLINE, capabilities={})
    raw, tok = PersonalToken.create_for_user(user=jj, label="haldimagi-mbp")
    PersonalToken.objects.filter(pk=tok.pk).update(revoked_at=timezone.now())   # the cleanup
    key = ec.generate_private_key(ec.SECP256R1())
    return {"user": jj, "ws": ws, "runner": runner, "raw": raw, "tok": tok, "key": key,
            "jwk": device_keys.normalize_public_jwk(header_jwk(key))}


def _ask(box, *, raw=None, key=None, signer=None, runner=None):
    key = key or box["key"]
    jwk = device_keys.normalize_public_jwk(header_jwk(key))
    return Client().post(
        URL, data={"runner": str((runner or box["runner"]).pk), "kind": "secure-enclave",
                   "public_jwk": jwk, "label": "Jonathans-MBP", "owner_token": raw or box["raw"]},
        content_type="application/json",
        HTTP_DPOP=make_dpop_proof(signer or key, "POST", HTU))


def test_a_stranded_box_gets_a_pending_key_and_its_owner_the_link(box, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = _ask(box)
    assert res.status_code == 201, res.content
    body = res.json()
    key = RunnerDeviceKey.objects.get(pk=body["id"])
    assert key.status == RunnerDeviceKey.PENDING and key.runner == box["runner"]
    assert body["approve_url"].endswith(f"device_key={key.pk}#device-keys")
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["jj@dimagi.com"]
    assert "haldimagi-mbp-cdp" in mail.outbox[0].subject
    # Asking again changes nothing and emails nobody.
    with django_capture_on_commit_callbacks(execute=True):
        assert _ask(box).status_code == 200
    assert len(mail.outbox) == 1


def test_it_never_reactivates_or_touches_an_existing_key(box):
    _ask(box)
    RunnerDeviceKey.objects.update(status=RunnerDeviceKey.REVOKED)
    res = _ask(box)
    assert res.status_code == 200 and res.json()["status"] == RunnerDeviceKey.REVOKED


def test_another_persons_token_or_an_old_one_is_refused(box):
    other = get_user_model().objects.create_user(username="x", email="x@dimagi.com")
    raw, _ = PersonalToken.create_for_user(user=other, label="x")
    assert _ask(box, raw=raw).status_code == 401
    assert _ask(box, raw="not-a-token").status_code == 401
    PersonalToken.objects.filter(pk=box["tok"].pk).update(revoked_at=timezone.now() - timedelta(days=31))
    assert _ask(box).status_code == 401
    assert not RunnerDeviceKey.objects.exists() and not mail.outbox


def test_a_deactivated_owner_gets_nothing(box):
    get_user_model().objects.filter(pk=box["user"].pk).update(is_active=False)
    assert _ask(box).status_code == 401


def test_the_box_must_hold_the_key_it_registers(box):
    stranger = ec.generate_private_key(ec.SECP256R1())
    assert _ask(box, signer=stranger).status_code == 401
    res = Client().post(URL, data={"runner": str(box["runner"].pk), "kind": "secure-enclave",
                                   "public_jwk": box["jwk"], "owner_token": box["raw"]},
                        content_type="application/json")            # no proof at all
    assert res.status_code == 401
    assert not RunnerDeviceKey.objects.exists()


def test_a_retired_runner_or_too_many_pending_keys_is_refused(box):
    for _ in range(3):
        assert _ask(box, key=ec.generate_private_key(ec.SECP256R1())).status_code == 201
    assert _ask(box).status_code == 429
    Runner.objects.filter(pk=box["runner"].pk).update(status=Runner.RETIRED)
    assert _ask(box, key=ec.generate_private_key(ec.SECP256R1())).status_code == 401
