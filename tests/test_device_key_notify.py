"""A pending runner device key tells the people who can approve it
(apps/harness/device_key_notify.py): the owner and explicit runner admins get an
email + Web Push linking to Settings → Your machines with the key highlighted —
once per new key, again only when an active key is sent back to pending — and
the box's own heartbeat reply says so for as long as the key waits.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner, RunnerAdmin, RunnerDeviceKey
from apps.push import services as push_services
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _jwk(key) -> dict:
    from canopy_sdk.keys import header_jwk

    return header_jwk(key)


@pytest.fixture
def box(monkeypatch):
    from apps.tokens.models import PersonalToken

    users = get_user_model().objects
    jj = users.create_user(username="jj", email="jj@dimagi.com")
    admin = users.create_user(username="ad", email="ad@dimagi.com")
    users.create_user(username="other", email="other@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    runner = Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
                                   workspace=ws, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAdmin.objects.create(runner=runner, user=admin)
    raw, _ = PersonalToken.create_for_user(user=jj, label="t")
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(push_services, "send_to_user",
                        lambda user, title, body, url: pushed.append((user.username, url)) or 1)
    return {"runner": runner, "pat": raw, "pushed": pushed}


def _register(box, key, ecdh=None):
    data = {"runner": str(box["runner"].pk), "public_jwk": _jwk(key), "kind": "secure-enclave", "label": "Mac"}
    if ecdh is not None:
        data["ecdh_public_jwk"] = _jwk(ecdh)
    return Client().post("/api/runner-device-keys/", data=data, content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {box['pat']}")


def test_a_new_pending_key_mails_and_pushes_the_owner_and_runner_admins(box, django_capture_on_commit_callbacks):
    key = ec.generate_private_key(ec.SECP256R1())
    with django_capture_on_commit_callbacks(execute=True):
        res = _register(box, key)
    assert res.status_code == 201
    kid = res.json()["id"]
    assert sorted(m.to[0] for m in mail.outbox) == ["ad@dimagi.com", "jj@dimagi.com"]
    msg = mail.outbox[0]
    assert "approve haldimagi-mbp-cdp" in msg.subject
    assert f"/settings?device_key={kid}#device-keys" in msg.body
    assert sorted(u for u, _ in box["pushed"]) == ["ad", "jj"]
    assert all(url.endswith(f"/settings?device_key={kid}#device-keys") for _, url in box["pushed"])


def test_re_registering_the_same_key_sends_nothing(box, django_capture_on_commit_callbacks):
    key = ec.generate_private_key(ec.SECP256R1())
    with django_capture_on_commit_callbacks(execute=True):
        _register(box, key)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert _register(box, key).status_code == 200
    assert mail.outbox == []


def test_an_active_key_with_a_new_ecdh_key_asks_for_re_approval(box, django_capture_on_commit_callbacks):
    key = ec.generate_private_key(ec.SECP256R1())
    with django_capture_on_commit_callbacks(execute=True):
        kid = _register(box, key).json()["id"]
    RunnerDeviceKey.objects.filter(pk=kid).update(status=RunnerDeviceKey.ACTIVE, approved_at=timezone.now())
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert _register(box, key, ecdh=ec.generate_private_key(ec.SECP256R1())).status_code == 200
    after = RunnerDeviceKey.objects.get(pk=kid)
    assert after.status == RunnerDeviceKey.ACTIVE and after.ecdh_waiting  # still signs in
    assert {m.to[0] for m in mail.outbox} == {"ad@dimagi.com", "jj@dimagi.com"}
    assert "stays online" in mail.outbox[0].body
    assert "needs re-approving" in mail.outbox[0].subject


def test_a_failing_notice_never_fails_the_registration(box, monkeypatch, django_capture_on_commit_callbacks):
    from apps.harness import device_key_notify

    monkeypatch.setattr(device_key_notify, "approvers", lambda runner: 1 / 0)
    with django_capture_on_commit_callbacks(execute=True):
        assert _register(box, ec.generate_private_key(ec.SECP256R1())).status_code == 201


def _heartbeat(box):
    res = Client().post(f"/api/harness/runners/{box['runner'].pk}/heartbeat", data={"active_turn_ids": []},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {box['pat']}")
    assert res.status_code == 200, res.content
    return res.json()


def test_the_heartbeat_tells_the_box_a_key_waits_until_it_is_approved(box):
    """The email is easy to miss; the box's own menu bar shows this field."""
    from apps.workspaces.models import WorkspaceMembership

    WorkspaceMembership.objects.create(user=box["runner"].owner, workspace=box["runner"].workspace,
                                       role=WorkspaceMembership.OWNER)
    assert _heartbeat(box)["key_waiting"] == ""
    key = ec.generate_private_key(ec.SECP256R1())
    kid = _register(box, key).json()["id"]
    assert _heartbeat(box)["key_waiting"].endswith(f"/settings?device_key={kid}#device-keys")
    RunnerDeviceKey.objects.filter(pk=kid).update(status=RunnerDeviceKey.ACTIVE, approved_at=timezone.now())
    assert _heartbeat(box)["key_waiting"] == ""
    _register(box, key, ecdh=ec.generate_private_key(ec.SECP256R1()))  # a new ECDH key waits again
    assert _heartbeat(box)["key_waiting"].endswith(f"/settings?device_key={kid}#device-keys")


def test_the_notice_names_the_computer_not_its_kernel(box, django_capture_on_commit_callbacks):
    """A default label is `host (platform.system())`; "Darwin" read to a Mac owner as a
    machine they had never heard of (2026-10-21). The notice says "the Mac named …"."""
    key = ec.generate_private_key(ec.SECP256R1())
    data = {"runner": str(box["runner"].pk), "public_jwk": _jwk(key), "kind": "secure-enclave",
            "label": "DIM-SA-A126 (Darwin)"}
    with django_capture_on_commit_callbacks(execute=True):
        assert Client().post("/api/runner-device-keys/", data=data, content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {box['pat']}").status_code == 201
    body = mail.outbox[0].body
    assert "the canopy runner on the Mac named DIM-SA-A126" in body
    assert "Darwin" not in body
    assert "asked by itself when it started" in body


@pytest.mark.parametrize("label,kind,said", [
    ("Jonathans-MBP (Darwin)", "secure-enclave", "the Mac named Jonathans-MBP"),
    ("DIM-SA-A126 (macOS)", "secure-enclave", "the Mac named DIM-SA-A126"),
    ("BOX (Windows)", "tpm", "the Windows PC named BOX"),
    ("cloud-ec2-1 instance role", "aws-iam", "cloud-ec2-1 instance role"),
    ("", "software", "a software key"),
])
def test_machine_phrases_a_key(label, kind, said):
    from apps.harness.device_key_notify import machine

    assert machine(RunnerDeviceKey(label=label, kind=kind)) == said
