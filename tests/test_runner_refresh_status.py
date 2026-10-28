"""A Refresh says where it is: pending and why, done and what changed, stuck and
whose it is.

The button gave a disabled "Refresh requested" and one muted line, then nothing:
no time, no reason it waited, no word when it finished. `RunnerOut.refresh` is
the request as a person reads it, the asker is told once when it finishes, and
one still pending after 30 minutes is the `runner_refresh` alert, handed to them.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_runner_refresh as src
from apps.harness import runner_refresh, runners
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

OLD = 1789990000.0


def _health(*, claude="2.1.1", cli="0.2.500", bootstrapped_at=OLD, bootstrap="ok", floor=0.0):
    return {
        "checks": [
            {"name": "claude.version", "status": "ok", "detail": f"Claude Code {claude}"},
            {"name": "canopy.cli", "status": "ok", "detail": f"canopy {cli}"},
            {"name": "bootstrap", "status": bootstrap, "detail": "" if bootstrap == "ok" else "line 9: boom"},
        ],
        "checked_at": bootstrapped_at,
        "bootstrapped_at": bootstrapped_at,
        "refresh_floor_until": floor,
    }


@pytest.fixture
def box(django_user_model, monkeypatch, django_capture_on_commit_callbacks):
    jj = django_user_model.objects.create_user(username="jj", email="jj@dimagi.com", password="x",
                                               first_name="Jonathan", last_name="Jackson")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    r = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=jj, workspace=ws,
                              status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                              code_version="0.4.0", code_sha="a" * 40)
    c = Client()
    c.force_login(jj)
    pushes = []
    monkeypatch.setattr("apps.harness.runner_refresh.send_to_user",
                        lambda user, title, body, url, *a, **k: pushes.append((title, url)) or True)
    monkeypatch.setattr(alerts, "SOURCES", ["apps.harness.alerts_runner_refresh"])
    monkeypatch.setattr(alerts, "send_to_user", lambda *a, **k: True)
    _beat(c, r, health=_health())
    # The finish notice and an alert's hand-off are sent after the commit.
    return {"jj": jj, "runner": r, "client": c, "pushes": pushes,
            "commits": lambda: django_capture_on_commit_callbacks(execute=True)}


def _beat(c, r, **body):
    body.setdefault("code_version", "0.4.0")
    body.setdefault("code_sha", "a" * 40)
    resp = c.post(f"/api/harness/runners/{r.id}/heartbeat",
                  data={"active_turn_ids": [], **body}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _refresh(box):
    resp = box["client"].post(f"/api/harness/runners/{box['runner'].id}/refresh")
    assert resp.status_code == 200, resp.content
    return resp.json()["refresh"]


def test_idle_says_nothing_was_asked(box):
    out = box["client"].get(f"/api/harness/runners/{box['runner'].id}/").json()
    assert out["refresh"]["state"] == "none"


def test_pending_with_nothing_in_the_way_says_who_and_when(box):
    st = _refresh(box)
    assert st["state"] == "pending"
    assert st["requested_by"] == "Jonathan Jackson"
    assert st["requested_at"]
    assert st["waiting_on"] == []


def test_pending_while_busy_counts_the_turns_in_flight(box):
    for _ in range(2):
        Turn.objects.create(status=Turn.RUNNING, claimed_by=box["runner"], project="canopy-web",
                            idempotency_key=uuid.uuid4().hex)
    st = _refresh(box)
    assert st["waiting_on"] == ["busy"] and st["in_flight"] == 2


def test_pending_within_the_floor_says_when_it_can_run(box):
    floor = timezone.now().timestamp() + 600
    _beat(box["client"], box["runner"], health=_health(floor=floor))
    st = _refresh(box)
    assert st["waiting_on"] == ["floor"]
    assert abs(timezone.datetime.fromisoformat(st["eligible_at"]).timestamp() - floor) < 1


def test_a_floor_already_past_is_not_a_reason(box):
    _beat(box["client"], box["runner"], health=_health(floor=timezone.now().timestamp() - 60))
    assert _refresh(box)["waiting_on"] == []


def test_pending_on_an_idle_box_that_cannot_restart_says_why(box):
    # cloud-ec2-2-marshellis, 2026-10-28: pending, waiting on nothing, for hours.
    health = _health()
    health["checks"].append({"name": "refresh", "status": "fail",
                             "detail": "could not restart to refresh: restart failed (exit 1)"})
    _beat(box["client"], box["runner"], health=health)
    st = _refresh(box)
    assert st["state"] == "pending" and st["waiting_on"] == []
    assert st["problems"] == ["refresh: could not restart to refresh: restart failed (exit 1)"]


def test_pending_on_a_dark_box_says_offline(box):
    Runner.objects.filter(pk=box["runner"].pk).update(last_heartbeat_at=timezone.now() - timedelta(hours=2))
    assert _refresh(box)["waiting_on"] == ["offline"]


def test_asking_again_while_pending_keeps_the_first_request(box):
    first = _refresh(box)
    again = _refresh(box)
    assert again["requested_at"] == first["requested_at"]


def test_done_shows_what_changed_and_tells_the_asker_once(box):
    _refresh(box)
    mail.outbox.clear()
    later = timezone.now().timestamp() + 5
    with box["commits"]():
        out = _beat(box["client"], box["runner"], code_version="0.4.1", code_sha="b" * 40,
                    health=_health(claude="2.1.5", cli="0.2.509", bootstrapped_at=later))
    st = out["refresh"]
    assert out["refresh_pending"] is False and st["state"] == "done"
    assert st["finished_at"] and st["took_seconds"] is not None
    assert {c["name"]: (c["before"], c["after"]) for c in st["changes"]} == {
        "runner code": (f"0.4.0 ({'a' * 12})", f"0.4.1 ({'b' * 12})"),
        "Claude Code": ("2.1.1", "2.1.5"),
        "canopy CLI": ("0.2.500", "0.2.509"),
    }
    assert st["problems"] == []
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["jj@dimagi.com"]
    assert "cloud-ec2-2 refreshed" in mail.outbox[0].subject
    assert "Claude Code: 2.1.1 → 2.1.5" in mail.outbox[0].body
    assert [url for _, url in box["pushes"]] == [f"/w/dimagi/runners/{box['runner'].pk}"]
    # A second beat with the same bootstrap tells no one again.
    with box["commits"]():
        _beat(box["client"], box["runner"], code_version="0.4.1", code_sha="b" * 40,
              health=_health(claude="2.1.5", cli="0.2.509", bootstrapped_at=later))
    assert len(mail.outbox) == 1


def test_done_with_a_failed_bootstrap_says_so(box):
    _refresh(box)
    mail.outbox.clear()
    with box["commits"]():
        out = _beat(box["client"], box["runner"],
                    health=_health(bootstrapped_at=timezone.now().timestamp() + 5, bootstrap="fail"))
    assert out["refresh"]["problems"] == ["bootstrap: line 9: boom"]
    assert "with problems" in mail.outbox[0].subject


def test_a_refresh_canopy_asked_for_tells_no_one(box):
    runners.request_refresh(box["runner"])  # a new agent's setup: no asker
    mail.outbox.clear()
    with box["commits"]():
        _beat(box["client"], box["runner"], health=_health(bootstrapped_at=timezone.now().timestamp() + 5))
    assert mail.outbox == []


def test_stuck_after_half_an_hour_is_an_alert_handed_to_the_asker(box):
    _refresh(box)
    r = box["runner"]
    Runner.objects.filter(pk=r.pk).update(refresh_requested_at=timezone.now() - timedelta(minutes=45))
    assert src.reconcile(timezone.now() - timedelta(minutes=20)) == []  # not yet stuck then
    mail.outbox.clear()
    with box["commits"]():
        alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    assert a.title == "cloud-ec2-2's refresh has not run yet"
    assert "Requested 45 min ago" in a.body
    assert a.handler_user_id == box["jj"].pk
    assert [x.id for x in src.actions(a, box["jj"])] == ["open", "dismiss"]
    assert len(mail.outbox) == 1  # the hand-off told the asker
    # A later tick, minutes on, is the same fact: no second notice.
    with box["commits"]():
        alerts.tick(timezone.now() + timedelta(minutes=5))
    assert len(mail.outbox) == 1
    # The box reports the bootstrap: the alert resolves itself.
    _beat(box["client"], r, health=_health(bootstrapped_at=timezone.now().timestamp() + 5))
    alerts.tick(timezone.now() + timedelta(minutes=10))
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED


def test_status_reads_versions_a_laptop_reports_through_its_clis(box):
    r = box["runner"]
    Runner.objects.filter(pk=r.pk).update(
        health={}, capabilities={"clis": {"claude-code": {"signed_in": True, "profiles": 0, "envelope": 0,
                                                     "version": "2.1.9"}}})
    r.refresh_from_db()
    assert runner_refresh.versions(r)["Claude Code"] == "2.1.9"


def test_canopys_own_cli_behind_ask_is_not_a_persons_request(box, monkeypatch):
    # `refresh_pending` also carries canopy's ask when the box's CLI is behind the
    # release; that has no asker, so the page shows no pending request for it.
    monkeypatch.setattr("apps.harness.models.canopy_release.refresh_owed", lambda health: True)
    out = box["client"].get(f"/api/harness/runners/{box['runner'].id}/").json()
    assert out["refresh_pending"] is True
    assert out["refresh"]["state"] == "none"
