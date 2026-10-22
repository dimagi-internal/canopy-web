"""The `turn_failure` alert source: a failure Ada could not handle.

An `open` investigation is Ada's (a debug turn has it) — still logged as an
error, never an alert. Once it is `escalated`, `debugger_failed` or `held` it
is a person's, and shows up where they look.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_turn_failure as src
from apps.harness.models import FailureInvestigation, Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_turn_failure"
NOTE = "Couldn't confirm your message was delivered to the session. Details: see the runner log."


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def fleet(monkeypatch):
    jj, member = _user("jj"), _user("member")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=member, role=WorkspaceMembership.VIEWER)
    Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, host="x", owner=jj, workspace=ws,
                          status=Runner.ONLINE, capabilities={})
    Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=jj)
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "member": member, "ws": ws}


def _inv(fleet, status=FailureInvestigation.ESCALATED, **kw):
    now = timezone.now()
    return FailureInvestigation.objects.create(
        workspace=fleet["ws"], fingerprint=kw.pop("fingerprint", "fp1"), sample_note=NOTE,
        first_seen=now, last_seen=now, status=status, occurrences=kw.pop("occurrences", 3),
        agents=kw.pop("agents", ["ace"]), runners=kw.pop("runners", ["cloud-ec2-2", "ghost-box"]), **kw)


def test_open_investigation_raises_nothing(fleet):
    _inv(fleet, status=FailureInvestigation.OPEN)
    _inv(fleet, status=FailureInvestigation.RESOLVED, fingerprint="fp2")
    assert src.reconcile(timezone.now()) == []


@pytest.mark.parametrize("status", ["escalated", "debugger_failed", "held"])
def test_escalated_raises_action_alert(fleet, status):
    inv = _inv(fleet, status=status)
    runner = Runner.objects.get(name="cloud-ec2-2")
    (r,) = src.reconcile(timezone.now())
    assert r.subject_key == f"investigation:{inv.pk}"
    assert r.workspace == "dimagi"
    assert r.title == ("Turns failing on cloud-ec2-2, ghost-box: "
                       "Couldn't confirm your message was delivered to the session.")
    assert r.scopes == ("workspace:dimagi", f"runner:{runner.pk}", "agent:ace")  # unknown runner skipped
    assert r.url == "/w/dimagi/agents/ace/turns"
    assert r.fingerprint == status
    assert src.SEVERITY == "action" and src.CONFIRM_WITHIN is None


def test_title_is_capped_and_url_falls_back(fleet):
    inv = _inv(fleet, agents=[], runners=[])
    inv.sample_note = "x" * 400
    inv.save()
    (r,) = src.reconcile(timezone.now())
    assert len(r.title.split(": ", 1)[1]) <= 120
    assert r.title.startswith("Turns failing on ")
    assert r.url == "/w/dimagi/activity"


def test_mark_resolved_resolves_investigation_and_alert(fleet):
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    ids = [a.id for a in alerts.visible_actions(alert, fleet["jj"])]
    assert ids == ["open", "mark_resolved", "dismiss"]
    alerts.act(alert, fleet["jj"], "mark_resolved")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.RESOLVED
    assert inv.resolved_by == fleet["jj"]
    assert inv.resolution_note == "Resolved from the alert"
    alert.refresh_from_db()
    assert alert.status == Alert.RESOLVED and alert.resolved_reason == Alert.REASON_ACTION


def test_non_log_reader_sees_nothing(fleet):
    _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    assert alerts.visible_actions(alert, fleet["member"]) == []


def _repeat(inv, note="Same failure again. Details differ."):
    """The real repeat path: `auto_debug._add_occurrence`, then the save its
    caller makes."""
    import types
    import uuid

    from apps.harness import auto_debug

    auto_debug._add_occurrence(inv, types.SimpleNamespace(pk=uuid.uuid4()), note, "ace", "cloud-ec2-2",
                               timezone.now())
    inv.save()


def test_dismiss_sticks_through_repeats_and_reopens_on_status_change(fleet):
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    alerts.act(alert, fleet["jj"], "dismiss")
    _repeat(inv)
    _repeat(inv)
    alerts.tick(timezone.now())
    alert.refresh_from_db()
    assert inv.occurrences == 5
    assert alert.status == Alert.DISMISSED  # a repeat is not a new fact
    inv.status = FailureInvestigation.DEBUGGER_FAILED
    inv.save()
    alerts.tick(timezone.now())
    alert.refresh_from_db()
    assert alert.status == Alert.OPEN  # a status change is


@pytest.mark.parametrize("status", ["open", "resolved"])
def test_mark_resolved_refuses_once_the_investigation_moved_on(fleet, status):
    """A debug turn took it back (`open`) or someone resolved it since the alert
    was raised: the stale alert must not clobber that."""
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    FailureInvestigation.objects.filter(pk=inv.pk).update(status=status, resolution_note="by the debugger")
    with pytest.raises(ValueError, match="no longer waiting on a person"):
        alerts.act(alert, fleet["jj"], "mark_resolved")
    inv.refresh_from_db()
    assert inv.status == status and inv.resolution_note == "by the debugger"
    alert.refresh_from_db()
    assert alert.status == Alert.OPEN
    assert not alert.events.filter(kind="acted").exists()  # rolled back with the refusal
