"""The alerts tick: reconcile every source, expire stale, notify new blocking."""
from __future__ import annotations

import datetime as dt
import sys
import types
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert, AlertEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

FAKE = "tests._fake_alert_source_tick"
KIND = "fake_tick_kind"
FIX = alerts.Action("fix", "Fix it", closes=True)
LOOK = alerts.Action("look", "Look", closes=False, href="/x")


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    WorkspaceMembership.objects.get_or_create(workspace=w, user=owner, defaults={"role": WorkspaceMembership.OWNER})
    return w


@pytest.fixture()
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: sent.append(user.pk) or False)
    return sent


@pytest.fixture()
def source(monkeypatch):
    mod = types.ModuleType(FAKE)
    mod.KIND = KIND
    mod.SEVERITY = Alert.BLOCKING
    mod.CONFIRM_WITHIN = None
    mod.CLOSING_ACTION_IDS = ("fix",)
    mod.offered = {}
    mod.reconcile = lambda now: []
    mod.actions = lambda alert, user: list(mod.offered.get(user.pk, []))
    mod.perform = lambda alert, user, action_id: None
    monkeypatch.setitem(sys.modules, FAKE, mod)
    monkeypatch.setattr(alerts, "SOURCES", [FAKE])
    return mod


def _raise(ws, **kw):
    d = dict(subject_key="t:1", workspace=ws.slug, title="Runner is down", url="/w/acme/r",
             scopes=("workspace:acme",))
    d.update(kw)
    return alerts.Raise(**d)


def _boom(now):
    raise RuntimeError("source is down")


def test_failing_source_resolves_nothing(source, ws, pushes):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() - dt.timedelta(minutes=5))
    source.reconcile = _boom
    alerts.tick()
    assert Alert.objects.get().status == Alert.OPEN


def test_working_source_resolves_what_it_no_longer_reports(source, ws, pushes):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() - dt.timedelta(minutes=5))
    alerts.tick()
    a = Alert.objects.get()
    assert (a.status, a.resolved_reason) == (Alert.RESOLVED, Alert.REASON_SOURCE)


def test_stale_alert_resolves_as_stale_even_if_source_fails(source, ws, pushes):
    source.CONFIRM_WITHIN = dt.timedelta(hours=1)
    source.reconcile = _boom
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() - dt.timedelta(hours=2))
    alerts.tick()
    a = Alert.objects.get()
    assert (a.status, a.resolved_reason) == (Alert.RESOLVED, Alert.REASON_STALE)


def test_fresh_alert_is_not_stale(source, ws, pushes):
    source.CONFIRM_WITHIN = dt.timedelta(hours=1)
    source.reconcile = _boom
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() - dt.timedelta(minutes=10))
    alerts.tick()
    assert Alert.objects.get().status == Alert.OPEN


def test_no_confirm_within_never_goes_stale(source, ws, pushes):
    source.reconcile = _boom
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() - dt.timedelta(days=30))
    alerts.tick()
    assert Alert.objects.get().status == Alert.OPEN


def test_blocking_notifies_closers_once(source, ws, owner, pushes):
    viewer = User.objects.create_user("viewer", "viewer@dimagi.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    source.offered = {owner.pk: [FIX], viewer.pk: [LOOK]}
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    alerts.tick()
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]
    a = Alert.objects.get()
    assert a.notified_at is not None
    assert pushes == [owner.pk]
    assert AlertEvent.objects.filter(alert=a, kind=AlertEvent.NOTIFIED).count() == 1


def test_blocking_notifies_closer_in_child_tree_member(source, ws, owner, pushes):
    child = Workspace.objects.create(slug="kid", display_name="Kid", parent=ws, created_by=owner)
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(child, workspace="kid")]
    alerts.tick()
    assert len(mail.outbox) == 1  # the org owner holds a closing action on the child's alert


def test_zero_recipients_still_marks_notified(source, ws, pushes):
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    alerts.tick()
    assert Alert.objects.get().notified_at is not None
    assert mail.outbox == []


def test_action_severity_does_not_notify(source, ws, owner, pushes):
    source.SEVERITY = Alert.ACTION
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    assert mail.outbox == [] and pushes == []
    assert Alert.objects.get().notified_at is None


def test_reopened_blocking_notifies_again(source, ws, owner, pushes):
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    source.reconcile = lambda now: []
    alerts.tick()
    assert Alert.objects.get().status == Alert.RESOLVED
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    assert len(mail.outbox) == 2


def test_tick_is_registered():
    from apps.common import ticks
    assert "alerts" in ticks.jobs()


def test_failed_delivery_is_retried_next_tick(source, ws, owner, monkeypatch):
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]
    monkeypatch.setattr(alerts, "_send", lambda *a, **k: "failed")
    monkeypatch.setattr(alerts, "send_to_user", lambda *a, **k: False)
    alerts.tick()
    assert Alert.objects.get().notified_at is None
    monkeypatch.setattr(alerts, "_send", lambda *a, **k: "sent")
    alerts.tick()
    assert Alert.objects.get().notified_at is not None


def test_overlapping_ticks_send_once(source, ws, owner, pushes):
    """Claim, then send: a tick that finds the claim already taken (another
    process stamped `notified_at` after this one read `pending`) sends nothing."""
    source.offered = {owner.pk: [FIX]}
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    real_filter = Alert.objects.filter

    class _Racing:
        """The first `pending` read returns the alert, then the other tick claims it."""

        def __init__(self):
            self.raced = False

        def __call__(self, *args, **kw):
            qs = real_filter(*args, **kw)
            if kw.get("notified_at__isnull") and kw.get("severity") == Alert.BLOCKING and not self.raced:
                self.raced = True
                rows = list(qs)
                real_filter(pk=a.pk).update(notified_at=timezone.now())  # the other tick wins
                return rows
            return qs

    with mock.patch.object(Alert.objects, "filter", _Racing()):
        alerts.notify_new_blocking(timezone.now())
    assert mail.outbox == [] and pushes == []
    assert not AlertEvent.objects.filter(alert=a, kind=AlertEvent.NOTIFIED).exists()


def test_failed_fanout_gives_the_claim_back(source, ws, owner, monkeypatch):
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(alerts, "notify_users", boom)
    alerts.tick()
    assert Alert.objects.get().notified_at is None  # retried next tick


def test_one_broken_source_does_not_stop_another(source, ws, owner, pushes, monkeypatch):
    other = types.ModuleType(FAKE + "2")
    other.KIND = "other_kind"
    other.SEVERITY = Alert.BLOCKING
    other.CONFIRM_WITHIN = None
    other.CLOSING_ACTION_IDS = ("fix",)
    other.reconcile = lambda now: [_raise(ws, subject_key="o:1", title="Other is down")]
    other.actions = lambda alert, user: [FIX]
    other.perform = lambda *a: None
    monkeypatch.setitem(sys.modules, FAKE + "2", other)
    monkeypatch.setattr(alerts, "SOURCES", [FAKE, "tests._no_such_source", FAKE + "2"])
    source.reconcile = _boom
    alerts.tick()
    b = Alert.objects.get(kind="other_kind")
    assert b.notified_at is not None
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]


def test_other_organisation_user_is_not_notified(source, ws, owner, pushes):
    other_owner = User.objects.create_user("zed", "zed@elsewhere.com", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    WorkspaceMembership.objects.get_or_create(workspace=other_ws, user=other_owner,
                                              defaults={"role": WorkspaceMembership.OWNER})
    source.offered = {owner.pk: [FIX], other_owner.pk: [FIX]}  # the source would offer both
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]
    assert pushes == [owner.pk]


def _assigned_only(user):
    from apps.push.models import NotificationPreference

    NotificationPreference.objects.create(user=user, alert_mail=NotificationPreference.ALERT_MAIL_ASSIGNED)


def test_a_closer_on_assigned_only_alert_mail_is_not_told(source, ws, owner, pushes):
    """An owner who holds the keys without wanting the owner's inbox (a superuser
    COO, say): the role fan-out skips them while someone else is told."""
    quiet = User.objects.create_user("coo", "coo@dimagi.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=quiet, role=WorkspaceMembership.OWNER)
    _assigned_only(quiet)
    source.offered = {owner.pk: [FIX], quiet.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]


def test_when_every_closer_chose_assigned_only_they_are_all_told(source, ws, owner, pushes):
    """Thinning out never reaches nobody."""
    _assigned_only(owner)
    source.offered = {owner.pk: [FIX]}
    source.reconcile = lambda now: [_raise(ws)]
    alerts.tick()
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]
