"""`Alert` / `AlertEvent` — the persisted unified-alert rows."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attention.models import Alert, AlertEvent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def ws():
    u = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    return Workspace.objects.create(slug="acme", display_name="Acme", created_by=u)


def _alert(ws, **kw):
    now = timezone.now()
    defaults = dict(
        kind="runner_credential",
        subject_key="runner:1:cred:a",
        workspace=ws,
        severity=Alert.ACTION,
        title="t",
        first_seen_at=now,
        last_seen_at=now,
    )
    defaults.update(kw)
    return Alert.objects.create(**defaults)


def test_alert_unique_per_kind_and_subject(ws):
    _alert(ws)
    with pytest.raises(IntegrityError), transaction.atomic():
        _alert(ws)
    # a different kind or subject is fine
    _alert(ws, kind="turn_failure")
    _alert(ws, subject_key="runner:2:cred:a")


def test_alert_defaults_and_constants(ws):
    a = _alert(ws)
    assert a.status == Alert.OPEN
    assert a.occurrences == 1
    assert a.scopes == []
    assert a.resolved_reason == "" and a.closed_by is None and a.notified_at is None
    assert Alert.SEVERITY_RANK == {"blocking": 0, "action": 1, "setup": 2}
    assert (Alert.BLOCKING, Alert.ACTION, Alert.SETUP) == ("blocking", "action", "setup")
    assert (Alert.OPEN, Alert.RESOLVED, Alert.DISMISSED) == ("open", "resolved", "dismissed")


def test_alert_event_ordering(ws):
    a = _alert(ws)
    t0 = timezone.now()
    AlertEvent.objects.create(alert=a, at=t0 + dt.timedelta(minutes=2), kind="resolved")
    AlertEvent.objects.create(alert=a, at=t0, kind="raised")
    AlertEvent.objects.create(alert=a, at=t0 + dt.timedelta(minutes=1), kind="dismissed")
    assert [e.kind for e in a.events.all()] == ["raised", "dismissed", "resolved"]
