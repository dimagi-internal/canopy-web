"""The `unclaimable_turns` alert source: queued turns no runner can ever take.

Configuration only — a turn waiting on an offline box is transient and raises
nothing. One alert per agent; cancelling the turns is the alert's own fix.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_unclaimable as src
from apps.harness import claim, initiator, services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_unclaimable"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _stuck_turn(agent, key):
    turn = services.enqueue_turn(initiator=initiator.system(via="test"), agent=agent,
                                 origin=Turn.ORIGIN_API, idempotency_key=key, prompt="hi")[0]
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - claim.UNCLAIMABLE_GRACE - dt.timedelta(seconds=5))
    return turn


@pytest.fixture
def fleet(monkeypatch):
    jj, ed, viewer = _user("jj"), _user("ed"), _user("viewer")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=ed, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=dimagi, user=viewer, role=WorkspaceMembership.VIEWER)
    # An online box that serves no agent: every agent turn is config-stuck.
    runner = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="cloud-ec2-1", owner=jj,
                                   workspace=dimagi, status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(), capabilities={"agents": []})
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=jj)
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "ed": ed, "viewer": viewer, "ws": dimagi, "runner": runner, "eva": eva}


def _alert():
    return Alert.objects.get(kind=src.KIND)


def test_config_stuck_turns_raise_one_alert_per_agent(fleet):
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=fleet["ws"], owner=fleet["jj"])
    t1, t2 = _stuck_turn(fleet["eva"], "a"), _stuck_turn(fleet["eva"], "b")
    _stuck_turn(hal, "c")
    out = {r.subject_key: r for r in src.reconcile(timezone.now())}
    assert set(out) == {"agent:eva:unclaimable", "agent:hal:unclaimable"}
    eva = out["agent:eva:unclaimable"]
    assert eva.title == "2 queued turns for Eva have no runner that can take them"
    assert out["agent:hal:unclaimable"].title == "1 queued turn for Hal has no runner that can take it"
    assert eva.workspace == "dimagi"
    assert eva.scopes == ("agent:eva", "workspace:dimagi")
    assert eva.url == "/w/dimagi/agents/eva/turns"
    before = eva.digest()
    assert len(before) <= 64
    t3 = _stuck_turn(fleet["eva"], "d")
    (again,) = [r for r in src.reconcile(timezone.now()) if r.subject_key == "agent:eva:unclaimable"]
    assert again.digest() != before  # a newly stuck turn is a change
    assert {t1.pk, t2.pk, t3.pk}


def test_offline_stuck_turns_raise_nothing(fleet):
    fleet["runner"].status = Runner.DISCONNECTED
    fleet["runner"].last_heartbeat_at = None
    fleet["runner"].save(update_fields=["status", "last_heartbeat_at"])
    RunnerAssignment.objects.create(agent=fleet["eva"], runner=fleet["runner"], rank=0)
    _stuck_turn(fleet["eva"], "a")
    (row,) = claim.unclaimable_queued_turns(ws_slugs={"dimagi"})
    assert row["kind"] == "offline"
    assert src.reconcile(timezone.now()) == []


def test_actions_are_for_people_who_may_cancel(fleet):
    _stuck_turn(fleet["eva"], "a")
    alerts.tick(timezone.now())
    a = _alert()
    acts = src.actions(a, fleet["ed"])
    assert [x.id for x in acts] == ["cancel", "route", "dismiss"]
    assert acts[0].closes and not acts[1].closes
    assert acts[1].href == "/w/dimagi/agents/eva/settings"
    assert src.actions(a, fleet["viewer"]) == []


def test_cancel_action_cancels_and_resolves(fleet):
    t1, t2 = _stuck_turn(fleet["eva"], "a"), _stuck_turn(fleet["eva"], "b")
    alerts.tick(timezone.now())
    alerts.act(_alert(), fleet["ed"], "cancel")
    assert set(Turn.objects.filter(pk__in=[t1.pk, t2.pk]).values_list("status", flat=True)) == {Turn.CANCELLED}
    a = _alert()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_ACTION
    alerts.tick(timezone.now())  # and the source agrees: nothing reopens
    assert _alert().status == Alert.RESOLVED
