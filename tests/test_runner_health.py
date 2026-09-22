"""A runner reports its own features, and can be asked to refresh itself.

cloud-ec2-1 read online + ready + code-current for six hours on 2026-09-22 while
it wrote no durable chat rows, read no mail and could not steer a turn: a failed
`git pull` skipped loading three packages, and the only record was a journald
line. `health` is the box saying which features work; `refresh` is the durable
request to re-run its bootstrap.
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.harness import services
from apps.harness.models import Runner

pytestmark = pytest.mark.django_db

HEALTH = {
    "checks": [
        {"name": "packages.transcripts", "status": "fail", "detail": "No module named 'canopy_transcript'"},
        {"name": "claude.credentials", "status": "warn", "detail": "one credential, no fallback"},
        {"name": "code_clone", "status": "ok", "detail": ""},
    ],
    "checked_at": 1790000000.0,
    "bootstrapped_at": 1789990000.0,
}


@pytest.fixture
def owner_and_runner(django_user_model):
    u = django_user_model.objects.create_user(username="jj", email="jj@dimagi.com", password="x")
    r = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=u,
                              status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    c = Client()
    c.force_login(u)
    return u, r, c


def _beat(c, r, **body):
    return c.post(f"/api/harness/runners/{r.id}/heartbeat",
                  data={"active_turn_ids": [], **body}, content_type="application/json")


def test_heartbeat_stores_health_and_serves_problems(owner_and_runner):
    _, r, c = owner_and_runner
    out = _beat(c, r, health=HEALTH).json()
    assert set(out["health_checks"]) == {"packages.transcripts", "claude.credentials", "code_clone"}
    assert out["health_checks"]["packages.transcripts"]["status"] == "fail"
    assert out["health_received_at"]
    assert out["health_bootstrapped_at"] == 1789990000.0


def test_a_beat_without_health_keeps_the_last_report(owner_and_runner):
    # The lease renewer beats without building the list; it must not wipe it.
    _, r, c = owner_and_runner
    _beat(c, r, health=HEALTH)
    out = _beat(c, r).json()
    assert out["health_checks"]["packages.transcripts"]["status"] == "fail"


def test_a_runner_that_never_reports_health_is_unknown_not_healthy(owner_and_runner):
    _, r, c = owner_and_runner
    out = _beat(c, r).json()
    assert out["health_checks"] is None


def test_malformed_health_is_rejected(owner_and_runner):
    _, r, c = owner_and_runner
    resp = _beat(c, r, health={"checks": [{"name": "x", "status": "maybe"}]})
    assert resp.status_code == 422


def test_refresh_is_pending_until_the_box_bootstraps_after_it(owner_and_runner):
    _, r, c = owner_and_runner
    _beat(c, r, health=HEALTH)
    resp = c.post(f"/api/harness/runners/{r.id}/refresh")
    assert resp.status_code == 200, resp.content
    assert resp.json()["refresh_pending"] is True
    # A beat reporting the SAME old bootstrap does not discharge it...
    assert _beat(c, r, health=HEALTH).json()["refresh_pending"] is True
    # ...a bootstrap that finished after the request does.
    later = timezone.now().timestamp() + 5
    out = _beat(c, r, health={**HEALTH, "bootstrapped_at": later}).json()
    assert out["refresh_pending"] is False


def test_refresh_needs_the_administer_tier(owner_and_runner, django_user_model):
    _, r, _ = owner_and_runner
    stranger = django_user_model.objects.create_user(username="s", password="x")
    c = Client()
    c.force_login(stranger)
    assert c.post(f"/api/harness/runners/{r.id}/refresh").status_code == 404
    r.refresh_from_db()
    assert r.refresh_requested_at is None


def test_refresh_pending_is_false_with_no_request(owner_and_runner):
    _, r, _ = owner_and_runner
    assert r.refresh_pending() is False
    services.request_refresh(r)
    assert r.refresh_pending() is True
