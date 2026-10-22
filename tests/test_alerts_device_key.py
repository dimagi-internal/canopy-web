"""The `device_key_pending` alert source: a runner's new device key waits for a
person, and the old "approve <runner>" mail steps aside where the alert covers it.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_device_key as src
from apps.harness.models import Runner, RunnerAdmin, RunnerDeviceKey
from apps.push import services as push_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_device_key"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _jwk(key) -> dict:
    from canopy_sdk.keys import header_jwk

    return header_jwk(key)


@pytest.fixture
def box(monkeypatch):
    from apps.tokens.models import PersonalToken

    jj, admin, other = _user("jj"), _user("ad"), _user("other")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    for u, role in ((jj, WorkspaceMembership.OWNER), (admin, WorkspaceMembership.EDITOR),
                    (other, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
    runner = Runner.objects.create(name="haldimagi-mbp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
                                   workspace=ws, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAdmin.objects.create(runner=runner, user=admin)
    raw, _ = PersonalToken.create_for_user(user=jj, label="t")
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    monkeypatch.setattr(push_services, "send_to_user", lambda *a, **k: 1)
    return {"jj": jj, "admin": admin, "other": other, "ws": ws, "runner": runner, "pat": raw}


def _key(runner, status=RunnerDeviceKey.PENDING, jkt="jkt-1"):
    return RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE, jkt=jkt,
                                          status=status, label="Mac")


def _register(box, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    return Client().post("/api/runner-device-keys/",
                         data={"runner": str(box["runner"].pk), "public_jwk": _jwk(key),
                               "kind": "secure-enclave", "label": "Mac"},
                         content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {box['pat']}")


def test_pending_key_raises_and_approval_resolves(box):
    key = _key(box["runner"])
    t0 = timezone.now()
    (r,) = src.reconcile(t0)
    assert r.subject_key == f"device_key:{key.pk}"
    assert r.workspace == "dimagi"
    assert r.title == "haldimagi-mbp has a new device key waiting for approval"
    assert r.url == f"/settings?device_key={key.pk}#device-keys"
    alerts.tick(t0)
    a = Alert.objects.get(kind=src.KIND)
    assert a.status == Alert.OPEN and a.severity == "blocking"
    owner = src.actions(a, box["jj"])
    assert [x.id for x in owner] == ["approve", "reject"]
    assert owner[0].closes and owner[0].href == r.url
    assert [x.id for x in src.actions(a, box["admin"])] == ["approve", "reject"]
    assert src.actions(a, box["other"]) == []
    key.status = RunnerDeviceKey.ACTIVE
    key.save(update_fields=["status"])
    alerts.tick(t0 + __import__("datetime").timedelta(minutes=1))
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_SOURCE


def test_private_runner_and_active_keys_raise_nothing(box):
    _key(box["runner"], status=RunnerDeviceKey.ACTIVE, jkt="a")
    _key(box["runner"], status=RunnerDeviceKey.REVOKED, jkt="b")
    private = Runner.objects.create(name="solo", kind=Runner.EMDASH, owner=box["jj"], status=Runner.ONLINE,
                                    workspace=None, capabilities={})
    _key(private, jkt="c")
    assert src.reconcile(timezone.now()) == []


def test_pending_key_no_longer_mails_directly(box, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert _register(box).status_code == 201
    assert mail.outbox == []  # the alert's own blocking notice reaches the approvers
    alerts.tick(timezone.now())
    assert sorted(m.to[0] for m in mail.outbox) == ["ad@dimagi.com", "jj@dimagi.com"]
    assert "waiting for approval" in mail.outbox[0].subject


def test_private_runner_keeps_the_pending_key_mail(box, django_capture_on_commit_callbacks):
    Runner.objects.filter(pk=box["runner"].pk).update(workspace=None)
    with django_capture_on_commit_callbacks(execute=True):
        assert _register(box).status_code == 201
    assert {m.to[0] for m in mail.outbox} == {"ad@dimagi.com", "jj@dimagi.com"}


def test_reject_revokes_the_pending_key_and_resolves(box):
    key = _key(box["runner"])
    alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    alerts.act(a, box["jj"], "reject")
    key.refresh_from_db()
    assert key.status == RunnerDeviceKey.REVOKED and key.revoked_by == box["jj"]
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_ACTION
