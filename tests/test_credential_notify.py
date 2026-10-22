"""A box's Claude credential alerts land, and reach a person.

cloud-ec2-fork, 2026-10-18: the box posted `claude_auth_required` to the flat
`POST /api/events/`, its owner was in two workspaces, so there was no default
workspace to file it in and it 422'd. Recorded or not, nothing but the admin
event log reads `runner.credential` rows, so nobody would have been told.
Now an alert posted with a runner token files in the runner's workspace, and
the credential alerts mail and push the runner's approvers once a day —
`claude_auth_required` only where the runner's signed-out probe check has not
already raised the `runner_credential` alert.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.events.models import Event
from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerAccessToken, RunnerAdmin, RunnerDeviceKey
from apps.push import services as push_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ROLE = "arn:aws:iam::123456789012:role/canopy-cloud-runner-fork-RunnerRole"


@pytest.fixture
def box(monkeypatch):
    users = get_user_model().objects
    jj = users.create_user(username="jj", email="jj@dimagi.com")
    admin = users.create_user(username="ad", email="ad@dimagi.com")
    ws_a = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    ws_b = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    for ws in (ws_a, ws_b):
        WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="cloud-ec2-fork", kind=Runner.CLOUD, owner=jj, workspace=ws_b,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={})
    RunnerAdmin.objects.create(runner=runner, user=admin)
    key = RunnerDeviceKey.objects.create(
        runner=runner, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
        jkt=aws_iam_proof.role_jkt(ROLE), status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(push_services, "send_to_user",
                        lambda user, title, body, url: pushed.append((user.username, url)) or 1)
    return {"user": jj, "runner": runner, "ws_b": ws_b, "token": raw, "pushed": pushed}


def _alert(kind="claude_usage_exhausted", key="usage-exhausted:cloud-ec2-fork"):
    return {"items": [{
        "source": "runner.credential", "kind": kind, "level": "error", "key": key,
        "summary": "Every Claude credential on cloud-ec2-fork is at its usage cap.",
    }]}


def _post(box, body, bearer=None):
    return Client().post("/api/events/", body, content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {bearer or box['token']}")


def test_runner_token_records_in_the_runners_workspace(box):
    res = _post(box, _alert())
    assert res.status_code == 200, res.content
    assert Event.objects.get().workspace_id == box["ws_b"].pk


def test_the_runners_workspace_is_used_only_where_the_owner_may_record(box):
    """The box was paired in `connect`; its owner is a viewer there now. The
    runner's workspace is then no answer, and the owner's default (none: two
    memberships) applies, exactly as for any caller."""
    WorkspaceMembership.objects.filter(workspace=box["ws_b"], user=box["user"]).update(
        role=WorkspaceMembership.VIEWER)
    res = _post(box, _alert())
    assert res.status_code == 422
    assert Event.objects.count() == 0
    assert mail.outbox == []


def test_the_owners_pat_still_needs_a_workspace(box):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=box["user"], label="t")
    res = _post(box, _alert(), bearer=raw)
    assert res.status_code == 422


def test_a_credential_event_mails_the_approvers(box):
    _post(box, _alert())
    assert len(mail.outbox) == 2
    assert sorted(m.to[0] for m in mail.outbox) == ["ad@dimagi.com", "jj@dimagi.com"]
    msg = mail.outbox[0]
    assert "cloud-ec2-fork" in msg.subject and "cloud-ec2-fork" in msg.body
    assert f"/supervisor?tab=runners&runner={box['runner'].pk}" in msg.body
    assert sorted(u for u, _ in box["pushed"]) == ["ad", "jj"]


def test_repeat_same_day_does_not_mail_again(box):
    _post(box, _alert())
    _post(box, _alert())
    assert len(mail.outbox) == 2
    assert Event.objects.get().count == 2


def test_the_next_day_mails_again(box):
    _post(box, _alert())
    Event.objects.update(last_seen_at=timezone.now() - dt.timedelta(days=1))
    _post(box, _alert())
    assert len(mail.outbox) == 4


def test_the_api_key_fallback_mails_too(box):
    _post(box, _alert(kind="claude_api_key_fallback", key="claude_api_key_fallback:cloud-ec2-fork"))
    assert len(mail.outbox) == 2


def test_auth_required_mails_a_runner_with_no_probe_checks(box):
    """A laptop posts no `claude.probe.*` checks: the alert cannot cover it, so
    the mail still does."""
    _post(box, _alert(kind="claude_auth_required", key="auth-required:cloud-ec2-fork"))
    assert sorted(m.to[0] for m in mail.outbox) == ["ad@dimagi.com", "jj@dimagi.com"]


def test_auth_required_mails_when_the_probe_was_inconclusive(box):
    box["runner"].health = {"checks": [{"name": "claude.probe.subscription-1", "status": "warn",
                                        "detail": "could not check: no answer within 60s"}]}
    box["runner"].save(update_fields=["health"])
    _post(box, _alert(kind="claude_auth_required", key="auth-required:cloud-ec2-fork"))
    assert len(mail.outbox) == 2


def test_auth_required_is_not_mailed_where_the_alert_covers_it(box):
    """A signed-out probe check is the `runner_credential` alert; its own
    blocking notice replaces this mail (tests/test_alerts_runner_credential.py)."""
    box["runner"].health = {"checks": [{"name": "claude.probe.subscription-1", "status": "fail",
                                        "detail": "not signed in — someone has to sign it in again"}]}
    box["runner"].save(update_fields=["health"])
    res = _post(box, _alert(kind="claude_auth_required", key="auth-required:cloud-ec2-fork"))
    assert res.status_code == 200
    assert Event.objects.get().kind == "claude_auth_required"
    assert mail.outbox == []
    assert box["pushed"] == []


def test_other_kinds_do_not_mail(box):
    _post(box, {"items": [{"source": "inbound.gmail", "kind": "gmail.push.missed",
                           "level": "error", "key": "eva@dimagi-ai.com", "summary": "x"}]})
    assert Event.objects.count() == 1
    assert mail.outbox == []
    assert box["pushed"] == []


def test_a_failed_notice_never_fails_the_recording(box, monkeypatch):
    from apps.harness import device_key_notify

    def boom(runner):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(device_key_notify, "approvers", boom)
    res = _post(box, _alert())
    assert res.status_code == 200
    assert Event.objects.count() == 1
