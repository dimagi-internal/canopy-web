"""/api/inbound/gmail/ — the doorbell: verification, resolution, ringing, auditing."""
from __future__ import annotations

import base64
import datetime as dt
import json
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.inbound import services
from apps.inbound.models import InboundMailbox
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

AUDIENCE = "https://labs.example/canopy/api/inbound/gmail/"
SIGNER = "push@project.iam.gserviceaccount.com"


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
    return ws


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="eva", name="Eva", workspace=workspace)


@pytest.fixture()
def mailbox(agent):
    return InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)


@pytest.fixture()
def runner(user, workspace):
    return Runner.objects.create(
        name="acedimagi-mbp-cdp",
        kind=Runner.EMDASH,
        paired_by=user,
        workspace=workspace,
        ready=True,
        status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(),
    )


def _assign(agent, runner, rank=0, **kw):
    return RunnerAssignment.objects.create(agent=agent, runner=runner, rank=rank, **kw)


def _envelope(address="eva@dimagi-ai.com", history_id="12345"):
    data = base64.b64encode(
        json.dumps({"emailAddress": address, "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "m1"}, "subscription": "sub"}


def _post(body=None, *, verified=True, settings_over=None):
    client = Client()
    over = {"INBOUND_PUSH_AUDIENCE": AUDIENCE, "INBOUND_PUSH_SERVICE_ACCOUNT": SIGNER}
    over.update(settings_over or {})
    claims = {"email": SIGNER, "email_verified": True, "aud": AUDIENCE}
    with mock.patch("django.conf.settings.INBOUND_PUSH_AUDIENCE", over["INBOUND_PUSH_AUDIENCE"]), \
         mock.patch(
             "django.conf.settings.INBOUND_PUSH_SERVICE_ACCOUNT",
             over["INBOUND_PUSH_SERVICE_ACCOUNT"],
         ):
        with mock.patch("google.oauth2.id_token.verify_oauth2_token") as v:
            if verified:
                v.return_value = claims
            else:
                v.side_effect = ValueError("bad signature")
            return client.post(
                "/api/inbound/gmail/",
                body if body is not None else _envelope(),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer fake.jwt.token",
            )


# ── verification ─────────────────────────────────────────────────────────────


def test_unverified_push_404s_not_403s(mailbox):
    """A probe must not learn the endpoint exists."""
    r = _post(verified=False)
    assert r.status_code == 404


def test_missing_bearer_is_refused(mailbox):
    with mock.patch("django.conf.settings.INBOUND_PUSH_AUDIENCE", AUDIENCE):
        r = Client().post(
            "/api/inbound/gmail/", _envelope(), content_type="application/json"
        )
    assert r.status_code == 404


def test_unconfigured_audience_refuses_everything(mailbox):
    """An unconfigured deployment must not quietly accept anonymous pushes."""
    r = _post(settings_over={"INBOUND_PUSH_AUDIENCE": ""})
    assert r.status_code == 404


def test_a_different_signer_is_refused(mailbox):
    """Audience alone is not identity."""
    with mock.patch("django.conf.settings.INBOUND_PUSH_AUDIENCE", AUDIENCE), \
         mock.patch("django.conf.settings.INBOUND_PUSH_SERVICE_ACCOUNT", SIGNER), \
         mock.patch("google.oauth2.id_token.verify_oauth2_token") as v:
        v.return_value = {"email": "someone-else@evil.example", "email_verified": True}
        r = Client().post(
            "/api/inbound/gmail/",
            _envelope(),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer fake.jwt.token",
        )
    assert r.status_code == 404


# ── the happy path ───────────────────────────────────────────────────────────


def test_push_rings_the_assigned_online_runner(mailbox, agent, runner):
    _assign(agent, runner)
    with mock.patch("apps.inbound.services.publish") as pub:
        r = _post()
    assert r.status_code == 200, r.content
    assert r.json()["ok"] is True
    assert r.json()["rang"] == ["acedimagi-mbp-cdp"]
    assert pub.call_count == 1
    group, message = pub.call_args[0]
    assert message == {"type": "runner.check_inbox", "mailbox": "eva@dimagi-ai.com"}


def test_push_stamps_last_push_at(mailbox, agent, runner):
    _assign(agent, runner)
    with mock.patch("apps.inbound.services.publish"):
        _post()
    mailbox.refresh_from_db()
    assert mailbox.last_push_at is not None


def test_push_logs_an_info_event(mailbox, agent, runner):
    _assign(agent, runner)
    with mock.patch("apps.inbound.services.publish"):
        _post()
    ev = Event.objects.get(kind="gmail.push")
    assert ev.level == "info"
    assert ev.payload["history_id"] == "12345"
    assert ev.payload["runners"] == ["acedimagi-mbp-cdp"]


def test_every_online_assigned_runner_is_rung(mailbox, agent, runner, user, workspace):
    """Cheap redundancy: the enqueue is idempotent, so a second read collapses."""
    second = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.REMOTE, paired_by=user, workspace=workspace,
        ready=True, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    _assign(agent, runner, rank=0)
    _assign(agent, second, rank=1)
    with mock.patch("apps.inbound.services.publish") as pub:
        r = _post()
    assert pub.call_count == 2
    assert set(r.json()["rang"]) == {"acedimagi-mbp-cdp", "cloud-ec2-1"}


def test_a_disabled_assignment_is_not_rung(mailbox, agent, runner):
    """A disabled runner never claims, so waking it spends a gog call for nothing."""
    _assign(agent, runner, enabled=False)
    with mock.patch("apps.inbound.services.publish") as pub:
        r = _post()
    assert pub.call_count == 0
    assert r.json()["reason"] == "no_runner"


def test_a_strict_source_rule_is_honoured(mailbox, agent, runner, user, workspace):
    """Routing composition is shared with claiming, so the doorbell cannot ring a
    runner that will never claim this turn."""
    other = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.REMOTE, paired_by=user, workspace=workspace,
        ready=True, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    _assign(agent, runner, rank=0)
    _assign(agent, other, rank=0, source=Turn.ORIGIN_EMAIL, strict=True)
    with mock.patch("apps.inbound.services.publish") as pub:
        r = _post()
    assert pub.call_count == 1
    assert r.json()["rang"] == ["cloud-ec2-1"]


# ── the loud failures ────────────────────────────────────────────────────────


def test_unknown_mailbox_is_200_but_logged(workspace):
    """200 because a 4xx makes Pub/Sub redeliver forever."""
    r = _post()
    assert r.status_code == 200
    assert r.json()["reason"] == "unknown_mailbox"
    assert Event.objects.filter(kind="gmail.push.unknown_mailbox", level="warn").exists()


def test_no_online_runner_is_logged_loudly(mailbox, agent, user, workspace):
    offline = Runner.objects.create(
        name="asleep", kind=Runner.EMDASH, paired_by=user, workspace=workspace,
        ready=True, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=2),
    )
    _assign(agent, offline)
    r = _post()
    assert r.json()["reason"] == "no_runner"
    ev = Event.objects.get(kind="gmail.push.no_runner")
    assert ev.level == "warn"


def test_a_malformed_payload_does_not_500(mailbox):
    r = _post({"message": {"data": "not-base64!!"}, "subscription": "s"})
    assert r.status_code == 200
    assert r.json()["reason"] == "no_address"


# ── the auditor: the poll catches what push missed ───────────────────────────


def _email_turn(agent, discovered_by):
    return Turn.objects.create(
        agent=agent,
        origin=Turn.ORIGIN_EMAIL,
        prompt="/eva:turn --thread abc",
        idempotency_key=f"email-eva-abc-{discovered_by}",
        origin_ref={"thread_id": "abc", "discovered_by": discovered_by},
    )


def test_poll_discovered_mail_with_a_live_watch_is_an_error(mailbox, agent):
    mailbox.watch_expires_at = timezone.now() + dt.timedelta(days=5)
    mailbox.save(update_fields=["watch_expires_at"])
    _email_turn(agent, "poll")
    ev = Event.objects.get(kind="gmail.push.missed")
    assert ev.level == "error"
    assert ev.payload["thread_id"] == "abc"


def test_push_discovered_mail_is_not_a_miss(mailbox, agent):
    mailbox.watch_expires_at = timezone.now() + dt.timedelta(days=5)
    mailbox.save(update_fields=["watch_expires_at"])
    _email_turn(agent, "push")
    assert not Event.objects.filter(kind="gmail.push.missed").exists()


def test_no_watch_means_no_miss(mailbox, agent):
    """With no watch registered there is nothing to have missed — logging every
    poll-discovered message would bury the real signal under the expected one."""
    _email_turn(agent, "poll")
    assert not Event.objects.filter(kind="gmail.push.missed").exists()


def test_an_expired_watch_does_not_spam_misses(mailbox, agent):
    mailbox.watch_expires_at = timezone.now() - dt.timedelta(hours=1)
    mailbox.save(update_fields=["watch_expires_at"])
    _email_turn(agent, "poll")
    assert not Event.objects.filter(kind="gmail.push.missed").exists()


def test_repeat_misses_coalesce_onto_one_row(mailbox, agent):
    mailbox.watch_expires_at = timezone.now() + dt.timedelta(days=5)
    mailbox.save(update_fields=["watch_expires_at"])
    for i in range(4):
        Turn.objects.create(
            agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
            idempotency_key=f"email-eva-t{i}",
            origin_ref={"thread_id": f"t{i}", "discovered_by": "poll"},
        )
    ev = Event.objects.get(kind="gmail.push.missed")
    assert ev.count == 4


# ── watch state ──────────────────────────────────────────────────────────────


def test_watch_expiring_soon_warns(mailbox):
    services.note_watch_state(mailbox, timezone.now() + dt.timedelta(hours=3))
    assert Event.objects.filter(kind="gmail.watch.expiring", level="warn").exists()


def test_watch_expired_errors(mailbox):
    services.note_watch_state(mailbox, timezone.now() - dt.timedelta(minutes=1))
    assert Event.objects.filter(kind="gmail.watch.expired", level="error").exists()


def test_a_healthy_watch_is_quiet(mailbox):
    services.note_watch_state(mailbox, timezone.now() + dt.timedelta(days=6))
    assert not Event.objects.filter(kind__startswith="gmail.watch").exists()
    mailbox.refresh_from_db()
    assert mailbox.watch_expires_at is not None
