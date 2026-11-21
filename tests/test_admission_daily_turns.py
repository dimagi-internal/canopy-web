"""An `agent_daily_turns` admission rule: a cap on how many turns one agent starts in
24 hours (what it may spend answering, say, a relay site's people).

Off unless someone adds the rule. Counted over the 24 hours before the turn, so the
answer at its claim is the answer at its enqueue; a turn a rule rejected never ran
and does not count.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from apps.admission import evaluate
from apps.admission.models import AdmissionRule
from apps.agents.models import Agent
from apps.harness import initiator as _initiator
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture
def w():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    M.objects.create(workspace=ws, user=jj, role=M.OWNER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, turn_mode=Agent.AUTO)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=jj, turn_mode=Agent.AUTO)
    client = Client()
    client.force_login(jj)
    return {"jj": jj, "ws": ws, "hal": hal, "echo": echo, "client": client}


_N = iter(range(10_000))


def _ask(agent, origin=Turn.ORIGIN_API):
    turn, _ = services.enqueue_turn(agent=agent, origin=origin, prompt="hi",
                                    idempotency_key=f"k-{next(_N)}",
                                    initiator=_initiator.system(via="test"))
    turn.refresh_from_db()
    return turn


def _cap(w, max_turns, **kw):
    return AdmissionRule.objects.create(workspace=w["ws"], kind=AdmissionRule.AGENT_DAILY_TURNS,
                                        max_turns=max_turns, reason="hal's daily budget",
                                        notify_owner=False, **kw)


def test_turns_past_the_cap_are_rejected_at_enqueue(w):
    _cap(w, 2, agent=w["hal"])
    first, second, third = _ask(w["hal"]), _ask(w["hal"]), _ask(w["hal"])
    assert first.status == second.status == Turn.QUEUED
    assert third.status == Turn.CANCELLED and "hal's daily budget" in third.result_note
    # A rejected turn never ran: it does not count toward the cap, so the next is rejected
    # for the two that did, not for three.
    assert evaluate.daily_turns(_ask(w["hal"])) == 2


def test_a_workspace_wide_cap_counts_each_agent_on_its_own(w):
    _cap(w, 1)
    assert _ask(w["hal"]).status == Turn.QUEUED
    assert _ask(w["echo"]).status == Turn.QUEUED
    assert _ask(w["hal"]).status == Turn.CANCELLED


def test_conditions_narrow_which_turns_the_cap_rejects(w):
    _cap(w, 1, origin=Turn.ORIGIN_SITE_MESSAGE)
    _ask(w["hal"])
    assert _ask(w["hal"]).status == Turn.QUEUED, "an api turn is not what the rule is about"
    assert _ask(w["hal"], origin=Turn.ORIGIN_SITE_MESSAGE).status == Turn.CANCELLED


def test_only_the_last_24_hours_count(w):
    _cap(w, 1, agent=w["hal"])
    old = _ask(w["hal"])
    Turn.objects.filter(pk=old.pk).update(created_at=old.created_at - dt.timedelta(hours=25))
    assert _ask(w["hal"]).status == Turn.QUEUED


def test_a_disabled_cap_rejects_nothing(w):
    _cap(w, 1, agent=w["hal"], enabled=False)
    _ask(w["hal"])
    assert _ask(w["hal"]).status == Turn.QUEUED


@pytest.mark.parametrize("bad", [
    {"max_turns": None},
    {"max_turns": 3, "action": AdmissionRule.ALLOW},
    {"max_turns": 3, "reach": AdmissionRule.OFFLINE},
])
def test_a_cap_must_be_a_reject_with_a_number(w, bad):
    with pytest.raises(ValidationError):
        AdmissionRule.objects.create(workspace=w["ws"], kind=AdmissionRule.AGENT_DAILY_TURNS,
                                     reason="r", **bad)


def test_only_a_cap_carries_a_number(w):
    with pytest.raises(ValidationError):
        AdmissionRule.objects.create(workspace=w["ws"], origin="email", max_turns=3, reason="r")


def test_the_api_adds_and_clears_a_cap(w):
    url = "/api/workspaces/connect/admission-rules"
    r = w["client"].post(url, {"agent": "hal", "kind": "agent_daily_turns", "max_turns": 50,
                               "reason": "budget"}, content_type="application/json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["kind"], body["max_turns"]) == ("agent_daily_turns", 50)
    assert "past 50 turns" in body["summary"]
    listed = w["client"].get(url).json()
    assert {"value": "agent_daily_turns"}.items() <= listed["choices"]["kinds"][1].items()
    r = w["client"].patch(f"{url}/{body['id']}", {"kind": "", "origin": "email"},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    assert (r.json()["kind"], r.json()["max_turns"]) == ("", None)
