"""The `turn_failure` alert source: one alert per failure, from the moment it is
recorded until it is fixed.

Every status but `resolved` is an open alert. The debugger agent holds it
(silent) until it is handed to a person (`escalated`), who is the one told.
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


def test_resolved_investigation_raises_nothing(fleet):
    _inv(fleet, status=FailureInvestigation.RESOLVED, fingerprint="fp2")
    assert src.reconcile(timezone.now()) == []


@pytest.fixture
def ada(fleet):
    return Agent.objects.create(slug="ada", name="Ada", workspace=fleet["ws"], owner=fleet["jj"])


@pytest.mark.parametrize("status", ["open", "debugger_failed", "held", "pending_deploy"])
def test_the_agent_stages_are_alerts_the_debugger_holds(fleet, ada, status):
    """They used to raise nothing (open) or raise as a person's (held,
    debugger_failed): an alert meant "needs a person" and had no assignee."""
    runner = Runner.objects.get(name="cloud-ec2-2")
    # pending_deploy only stays so while a runner it hit is known to lack the fix.
    Runner.objects.filter(pk=runner.pk).update(code_sha="aaaaaaa1", code_committed_at=1)
    inv = _inv(fleet, status=status, fix_commit="bbbbbbb2", fix_committed_at=timezone.now())
    (r,) = src.reconcile(timezone.now())
    assert r.subject_key == f"investigation:{inv.pk}"
    assert r.workspace == "dimagi"
    lead = "Fix pending deploy on" if status == "pending_deploy" else "Turns keep failing on"
    assert r.title == (f"{lead} cloud-ec2-2, ghost-box: "
                       "Couldn't confirm your message was delivered to the session.")
    assert r.scopes == ("workspace:dimagi", f"runner:{runner.pk}", "agent:ace")  # unknown runner skipped
    assert r.url == "/w/dimagi/agents/ace/turns"
    assert (r.handler_agent, r.handler_user) == (ada.pk, None)
    assert r.fingerprint == f"{status}:{ada.pk}:"
    assert src.SEVERITY == "action" and src.CONFIRM_WITHIN is None


def test_escalated_is_the_persons_with_the_ask(fleet, ada):
    _inv(fleet, handler=fleet["jj"], ask="Sign Claude back in on cloud-ec2-2.")
    (r,) = src.reconcile(timezone.now())
    assert (r.handler_agent, r.handler_user) == (None, fleet["jj"].pk)
    assert r.title.startswith("Needs you — turns keep failing on cloud-ec2-2")
    assert r.body.startswith("Sign Claude back in on cloud-ec2-2.") and r.ask == r.body.split("\n")[0]


def test_an_agent_held_alert_is_silent_and_a_hand_off_tells_one_person(fleet, ada, monkeypatch,
                                                                         django_capture_on_commit_callbacks):
    """The rule on the framework, not just this source: a BLOCKING alert an agent
    holds notifies no one on the tick; handing it to a person notifies them once."""
    monkeypatch.setattr(src, "SEVERITY", "blocking")
    inv = _inv(fleet, status=FailureInvestigation.OPEN)
    with django_capture_on_commit_callbacks(execute=True):
        alerts.tick(timezone.now())
        alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    assert alert.handler_agent_id == ada.pk
    assert not alert.events.filter(kind="notified").exists()

    inv.status, inv.handler, inv.ask = FailureInvestigation.ESCALATED, fleet["jj"], "Look at cloud-ec2-2."
    inv.save()
    with django_capture_on_commit_callbacks(execute=True):
        alerts.tick(timezone.now())
        alerts.tick(timezone.now())
    alert.refresh_from_db()
    assert alert.handler_user_id == fleet["jj"].pk and alert.handler_agent_id is None
    assert [e.payload["to"] for e in alert.events.filter(kind="notified")] == [[fleet["jj"].pk]]


def test_title_is_capped_and_url_falls_back(fleet):
    inv = _inv(fleet, status=FailureInvestigation.HELD, agents=[], runners=[])
    inv.sample_note = "x" * 400
    inv.save()
    (r,) = src.reconcile(timezone.now())
    assert len(r.title.split(": ", 1)[1]) <= 120
    assert r.title.startswith("Turns keep failing on ")
    assert r.url == "/w/dimagi/activity?status=failed"


def test_mark_resolved_resolves_investigation_and_alert(fleet):
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    ids = [a.id for a in alerts.visible_actions(alert, fleet["jj"])]
    assert ids == ["open", "mark_resolved"]
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


def test_dismiss_sticks_through_repeats_and_reopens_on_status_change(fleet, django_capture_on_commit_callbacks):
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    alerts.dismiss(alert, fleet["jj"])  # not offered on screen; pins the fingerprint rule
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


@pytest.mark.parametrize("status", ["resolved"])
def test_mark_resolved_refuses_once_the_investigation_moved_on(fleet, status):
    """Someone resolved it since the alert was raised: the stale alert must not
    clobber that."""
    inv = _inv(fleet)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    FailureInvestigation.objects.filter(pk=inv.pk).update(status=status, resolution_note="by the debugger")
    with pytest.raises(ValueError, match="already resolved"):
        alerts.act(alert, fleet["jj"], "mark_resolved")
    inv.refresh_from_db()
    assert inv.status == status and inv.resolution_note == "by the debugger"
    alert.refresh_from_db()
    assert alert.status == Alert.OPEN
    assert not alert.events.filter(kind="acted").exists()  # rolled back with the refusal
