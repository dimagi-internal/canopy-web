"""The alert core service — `apps/attention/alerts.py`: reconcile, dismiss, act.

A fake source module is registered for each test (monkeypatched `SOURCES`), so
these tests pin the framework's lifecycle without depending on any real source.
"""
from __future__ import annotations

import datetime as dt
import sys
import types
from importlib import import_module

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert, AlertEvent
from apps.events.models import Event
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

FAKE = "tests._fake_alert_source"
KIND = "fake_kind"


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    return Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)


@pytest.fixture()
def source(monkeypatch):
    """A fake source: `offered` maps user pk → the actions that user is offered."""
    mod = types.ModuleType(FAKE)
    mod.KIND = KIND
    mod.SEVERITY = Alert.BLOCKING
    mod.CONFIRM_WITHIN = None
    mod.CLOSING_ACTION_IDS = ("fix",)
    mod.offered = {}
    mod.performed = []
    mod.reconcile = lambda now: []
    mod.actions = lambda alert, user: list(mod.offered.get(user.pk, []))
    mod.perform = lambda alert, user, action_id: mod.performed.append((alert.pk, user.pk, action_id))
    monkeypatch.setitem(sys.modules, FAKE, mod)
    monkeypatch.setattr(alerts, "SOURCES", [FAKE])
    return mod


def _raise(ws, **kw):
    defaults = dict(subject_key="thing:1", workspace=ws.slug, title="Token hal is signed out",
                    url="/w/acme/runners/1", scopes=("workspace:acme",))
    defaults.update(kw)
    return alerts.Raise(**defaults)


def _kinds(alert):
    return [e.kind for e in AlertEvent.objects.filter(alert=alert).order_by("at", "pk")]


def test_reconcile_raises_once_and_is_quiet_on_repeat(source, ws):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    t1 = t0 + dt.timedelta(minutes=5)
    alerts.apply_reconcile(KIND, [_raise(ws)], t1)

    assert Alert.objects.count() == 1
    a = Alert.objects.get()
    assert (a.kind, a.subject_key, a.workspace_id, a.severity) == (KIND, "thing:1", "acme", Alert.BLOCKING)
    assert a.status == Alert.OPEN
    assert a.scopes == ["workspace:acme"]
    assert a.fingerprint  # empty fingerprint → digest of title + body
    assert a.first_seen_at == t0 and a.last_seen_at == t1
    assert _kinds(a) == [AlertEvent.RAISED]
    # mirrored into the coalescing event log
    ev = Event.objects.get(source="alert", key=str(a.pk))
    assert ev.kind == "alert.raised" and ev.level == Event.WARN and ev.workspace_id == "acme"


def test_reconcile_changed_fingerprint_updates_open_alert(source, ws):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    alerts.apply_reconcile(KIND, [_raise(ws, title="Token hal is capped")], t0 + dt.timedelta(minutes=1))
    a = Alert.objects.get()
    assert a.title == "Token hal is capped" and a.status == Alert.OPEN
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.CHANGED]


def test_reconcile_resolves_missing(source, ws):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    alerts.apply_reconcile(KIND, [], t0 + dt.timedelta(minutes=1))
    a = Alert.objects.get()
    assert a.status == Alert.RESOLVED
    assert a.resolved_reason == Alert.REASON_SOURCE
    assert a.closed_by is None and a.closed_at is not None
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.RESOLVED]
    assert AlertEvent.objects.get(alert=a, kind=AlertEvent.RESOLVED).payload == {"reason": "source"}


def test_reconcile_only_resolves_its_own_kind(source, ws):
    now = timezone.now()
    other = Alert.objects.create(kind="other_kind", subject_key="x", workspace=ws, severity=Alert.SETUP,
                                 title="t", first_seen_at=now, last_seen_at=now)
    alerts.apply_reconcile(KIND, [], now)
    other.refresh_from_db()
    assert other.status == Alert.OPEN


def test_reconcile_reraise_of_resolved_alert_reopens_it(source, ws):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    alerts.apply_reconcile(KIND, [], t0 + dt.timedelta(minutes=1))
    a = Alert.objects.get()
    a.notified_at = t0
    a.save(update_fields=["notified_at"])
    alerts.apply_reconcile(KIND, [_raise(ws)], t0 + dt.timedelta(minutes=2))
    a.refresh_from_db()
    assert Alert.objects.count() == 1
    assert a.status == Alert.OPEN and a.resolved_reason == "" and a.closed_at is None
    assert a.notified_at is None
    assert a.occurrences == 2
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.RESOLVED, AlertEvent.REOPENED]


def test_dismiss_sticks_until_fingerprint_changes(source, ws, owner):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws, fingerprint="v1")], t0)
    a = Alert.objects.get()
    a.notified_at = t0
    a.save(update_fields=["notified_at"])
    alerts.dismiss(a, owner)
    a.refresh_from_db()
    assert a.status == Alert.DISMISSED and a.closed_by == owner

    alerts.apply_reconcile(KIND, [_raise(ws, fingerprint="v1")], t0 + dt.timedelta(minutes=1))
    a.refresh_from_db()
    assert a.status == Alert.DISMISSED
    assert a.last_seen_at == t0 + dt.timedelta(minutes=1)

    alerts.apply_reconcile(KIND, [_raise(ws, fingerprint="v2")], t0 + dt.timedelta(minutes=2))
    a.refresh_from_db()
    assert a.status == Alert.OPEN
    assert a.notified_at is None and a.closed_by is None and a.closed_at is None
    assert a.fingerprint == "v2"
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.DISMISSED, AlertEvent.REOPENED]


def test_dismissed_alert_resolves_when_condition_clears(source, ws, owner):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    a = Alert.objects.get()
    alerts.dismiss(a, owner)
    alerts.apply_reconcile(KIND, [], timezone.now() + dt.timedelta(minutes=1))
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_SOURCE
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.DISMISSED, AlertEvent.RESOLVED]


def test_dismissed_then_cleared_then_recurs_with_same_fingerprint_reopens(source, ws, owner):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws, fingerprint="v1")], t0)
    a = Alert.objects.get()
    alerts.dismiss(a, owner)
    t1 = timezone.now() + dt.timedelta(minutes=1)
    alerts.apply_reconcile(KIND, [], t1)
    alerts.apply_reconcile(KIND, [_raise(ws, fingerprint="v1")], t1 + dt.timedelta(minutes=1))
    a.refresh_from_db()
    assert a.status == Alert.OPEN
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.DISMISSED, AlertEvent.RESOLVED, AlertEvent.REOPENED]


def test_dismissed_alert_still_true_stays_dismissed(source, ws, owner):
    t0 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    a = Alert.objects.get()
    alerts.dismiss(a, owner)
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now() + dt.timedelta(minutes=1))
    a.refresh_from_db()
    assert a.status == Alert.DISMISSED
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.DISMISSED]


def test_stale_snapshot_does_not_reopen_an_alert_closed_after_it(source, ws, owner):
    """A tick computes its snapshot at `now`; a person closes the alert while
    it runs; applying that snapshot must not reopen (and re-notify) it."""
    t0 = timezone.now() - dt.timedelta(minutes=5)
    alerts.apply_reconcile(KIND, [_raise(ws)], t0)
    a = Alert.objects.get()
    snapshot_at = timezone.now()
    source.offered[owner.pk] = [alerts.Action("fix", "Fix it", closes=True)]
    alerts.act(a, owner, "fix")  # closed_at is after snapshot_at
    alerts.apply_reconcile(KIND, [_raise(ws)], snapshot_at)
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED and a.closed_by == owner
    assert AlertEvent.REOPENED not in _kinds(a)


def test_stale_snapshot_does_not_resolve_a_newer_confirmation(source, ws):
    t1 = timezone.now()
    alerts.apply_reconcile(KIND, [_raise(ws)], t1)
    alerts.apply_reconcile(KIND, [], t1 - dt.timedelta(seconds=1))  # older snapshot, applied late
    assert Alert.objects.get().status == Alert.OPEN


def test_concurrent_create_falls_through_to_update(source, ws, monkeypatch):
    """Another reconcile creates the subject between our lookup and our create:
    the unique-constraint error must not abort this reconcile."""
    t0 = timezone.now()
    real = alerts._find_locked
    calls = []

    def racing(kind, subject_key):
        if not calls:
            calls.append(1)
            Alert.objects.create(kind=kind, subject_key=subject_key, workspace=ws, severity=Alert.BLOCKING,
                                 title="old", fingerprint="old", first_seen_at=t0, last_seen_at=t0)
            return None
        return real(kind, subject_key)

    monkeypatch.setattr(alerts, "_find_locked", racing)
    later = t0 + dt.timedelta(minutes=1)
    alerts.apply_reconcile(KIND, [_raise(ws), _raise(ws, subject_key="thing:2")], later)
    assert Alert.objects.count() == 2
    a = Alert.objects.get(subject_key="thing:1")
    assert a.title == "Token hal is signed out" and a.last_seen_at == later
    assert _kinds(a) == [AlertEvent.CHANGED]


def test_offers_dismiss_ignores_status(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    source.offered[owner.pk] = [alerts.DISMISS]
    alerts.dismiss(a, owner)
    assert alerts.visible_actions(a, owner) == []
    assert alerts.offers_dismiss(a, owner) is True
    assert alerts.offers_dismiss(a, other) is False


def test_action_shapes_are_exclusive():
    with pytest.raises(ValueError):
        alerts.Action("ask", "Ask", closes=True, notify=(1,))
    with pytest.raises(ValueError):
        alerts.Action("ask", "Ask", closes=False, href="/w/acme/x", notify=(1,))
    alerts.Action("ask", "Ask", closes=False, notify=(1,))
    alerts.Action("open", "Open", closes=True, href="/w/acme/x")


def test_undismiss_reopens_and_logs(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    alerts.dismiss(a, owner)
    alerts.undismiss(a, owner)
    a.refresh_from_db()
    assert a.status == Alert.OPEN and a.closed_by is None and a.closed_at is None
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.DISMISSED, AlertEvent.UNDISMISSED]
    assert AlertEvent.objects.get(alert=a, kind=AlertEvent.UNDISMISSED).actor == owner


def test_act_not_offered_raises(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    source.offered[owner.pk] = [alerts.Action("fix", "Fix it", closes=True)]
    with pytest.raises(alerts.ActionNotOffered):
        alerts.act(a, other, "fix")
    assert not AlertEvent.objects.filter(alert=a, kind=AlertEvent.ACTED).exists()
    assert source.performed == []


def test_closing_handler_resolves_with_actor(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("fix", "Fix it", closes=True)]
    out = alerts.act(a, owner, "fix")
    assert out.status == Alert.RESOLVED
    assert out.resolved_reason == Alert.REASON_ACTION
    assert out.closed_by == owner
    assert source.performed == [(a.pk, owner.pk, "fix")]
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.ACTED, AlertEvent.RESOLVED]
    acted = AlertEvent.objects.get(alert=a, kind=AlertEvent.ACTED)
    assert acted.actor == owner and acted.action_id == "fix"


def test_non_closing_handler_leaves_alert_open(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("poke", "Poke it", closes=False)]
    out = alerts.act(a, owner, "poke")
    assert out.status == Alert.OPEN
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.ACTED]


def test_failing_handler_writes_nothing(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("fix", "Fix it", closes=True)]

    def boom(alert, user, action_id):
        raise RuntimeError("handler failed")

    source.perform = boom
    with pytest.raises(RuntimeError):
        alerts.act(a, owner, "fix")
    a.refresh_from_db()
    assert a.status == Alert.OPEN
    assert _kinds(a) == [AlertEvent.RAISED]


def test_dismiss_action_is_handled_by_the_framework(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.DISMISS]
    out = alerts.act(a, owner, "dismiss")
    assert out.status == Alert.DISMISSED and out.closed_by == owner
    assert source.performed == []  # never source.perform
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.ACTED, AlertEvent.DISMISSED]


def test_link_action_is_not_acted_server_side(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("open", "Open it", closes=True, href="/w/acme/x")]
    with pytest.raises(alerts.ActionNotOffered):
        alerts.act(a, owner, "open")
    assert _kinds(a) == [AlertEvent.RAISED]


def test_act_on_closed_alert_is_not_offered(source, ws, owner):
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("fix", "Fix it", closes=True), alerts.DISMISS]
    alerts.dismiss(a, owner)
    assert alerts.visible_actions(a, owner) == []
    with pytest.raises(alerts.ActionNotOffered):
        alerts.act(a, owner, "fix")


def test_notify_action_emails_and_stays_open(source, ws, owner, settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example.com/"
    fixer = User.objects.create_user("fixer", "fixer@dimagi.com", "pw")
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    source.offered[owner.pk] = [alerts.Action("ask", "Ask fixer to fix it", closes=False, notify=(fixer.pk,))]
    out = alerts.act(a, owner, "ask")
    assert out.status == Alert.OPEN
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["fixer@dimagi.com"]
    assert "Token hal is signed out" in msg.subject
    assert "https://canopy.example.com/w/acme/runners/1" in msg.alternatives[0][0]
    assert _kinds(a) == [AlertEvent.RAISED, AlertEvent.ACTED, AlertEvent.NOTIFIED]
    notified = AlertEvent.objects.get(alert=a, kind=AlertEvent.NOTIFIED)
    assert notified.payload == {"to": [fixer.pk], "by": owner.pk}
    assert notified.actor == owner


def test_notify_users_returns_count(source, ws):
    a1 = User.objects.create_user("a1", "a1@dimagi.com", "pw")
    a2 = User.objects.create_user("a2", "a2@dimagi.com", "pw")
    alerts.apply_reconcile(KIND, [_raise(ws)], timezone.now())
    a = Alert.objects.get()
    assert alerts.notify_users(a, [a1, a2]) == 2
    ev = AlertEvent.objects.get(alert=a, kind=AlertEvent.NOTIFIED)
    assert ev.payload == {"to": [a1.pk, a2.pk], "by": None} and ev.actor is None


def test_source_for_finds_registered_module(source):
    assert alerts.source_for(KIND) is source
    with pytest.raises(LookupError):
        alerts.source_for("nope")


def test_every_registered_source_declares_a_closing_action():
    for path in alerts.SOURCES:
        mod = import_module(path)
        assert mod.CLOSING_ACTION_IDS, f"{path} declares no closing action"
        assert mod.SEVERITY in Alert.SEVERITY_RANK, f"{path} has unknown severity {mod.SEVERITY!r}"
