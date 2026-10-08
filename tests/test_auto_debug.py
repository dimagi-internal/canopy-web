"""A failed turn starts a debugger turn — and the brakes that keep it from looping.

See apps/harness/auto_debug.py. Jonathan, 2026-10-07: an ACE Slack turn failed
("Couldn't confirm your message was delivered…") and nothing looked at it.
"""
from __future__ import annotations

import datetime as dt
import itertools
import uuid
from unittest import mock

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import Event
from apps.harness import auto_debug, services
from apps.harness import initiator as who
from apps.harness.models import FailureInvestigation, Runner, RunnerDrill, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_user, a_workspace

pytestmark = pytest.mark.django_db

NOT_RECEIVED = (
    'Couldn\'t confirm your message was delivered. It was typed into the emdash session '
    '"{task}", but it hasn\'t shown up in that session\'s transcript, so it may or may not '
    'have arrived. Check the session before sending it again, so it doesn\'t land twice.'
)

_seq = itertools.count()


@pytest.fixture
def ws():
    return a_workspace()


@pytest.fixture
def ada(ws):
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=a_user())


@pytest.fixture
def runner():
    return Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=a_member())


def _agent(ws, slug):
    return Agent.objects.get_or_create(slug=slug, defaults={"name": slug, "workspace": ws})[0]


def _fail(agent, runner, note, *, session_key=None, origin="slack", origin_ref=None):
    """A turn that ran (it has a session) and ended FAILED through finish_turn."""
    n = next(_seq)
    turn = Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=f"t-{n}-{uuid.uuid4()}",
        status=Turn.RUNNING, claimed_by=runner, claimed_at=timezone.now(),
        session_key=session_key if session_key is not None else f"c-{agent.slug}-slack-{4000 + n}",
        origin_ref=origin_ref or {},
    )
    return services.finish_turn(turn, status=Turn.FAILED, result_note=note)


def _debug_turns():
    return Turn.objects.filter(idempotency_key__startswith=auto_debug.KEY_PREFIX).order_by("created_at")


def _finish_debug_turns(status=Turn.DONE):
    """Let the debugger's queued turns end, so the next one is not blocked behind
    the one-executing-turn-per-agent rule (irrelevant here, but realistic)."""
    for t in _debug_turns().filter(status=Turn.QUEUED):
        Turn.objects.filter(pk=t.pk).update(status=status, finished_at=timezone.now())


# ---- the happy path ----------------------------------------------------------------


def test_a_failed_turn_triggers_the_debugger_with_a_self_contained_prompt(ada, ws, runner):
    ace = _agent(ws, "ace")
    failed = _fail(ace, runner, NOT_RECEIVED.format(task="c-ace-slack-4795"), session_key="c-ace-slack-4795")

    inv = FailureInvestigation.objects.get()
    assert inv.status == FailureInvestigation.OPEN and inv.occurrences == 1
    assert inv.agents == ["ace"] and inv.runners == ["haldimagi-mbp-cdp"]
    debug = _debug_turns().get()
    assert inv.debug_turn_id == debug.pk
    assert debug.agent_id == ada.pk and debug.origin == Turn.ORIGIN_API
    assert debug.idempotency_key == f"auto-debug:{inv.pk}:1"
    assert debug.origin_ref["auto_debug"] == inv.pk
    assert debug.initiator_kind == who.SYSTEM and debug.initiator_via == "auto-debug"
    assert debug.parent_turn_id == failed.pk

    first, *rest = debug.prompt.split("\n")
    assert first == f"/ada:debug-failure --investigation {inv.pk}"
    body = "\n".join(rest)
    for fact in (str(failed.pk), "Agent: ace", "Runner: haldimagi-mbp-cdp", "Origin: slack",
                 "c-ace-slack-4795", "Occurrences: 1", NOT_RECEIVED.format(task="c-ace-slack-4795"),
                 f"/api/harness/turns/{failed.pk}", f"/w/{ws.slug}/agents/ace/turns"):
        assert fact in body, fact


def test_the_same_failure_on_other_agents_and_sessions_only_counts(ada, ws, runner):
    for slug in ("ace", "echo", "eva"):
        _fail(_agent(ws, slug), runner, NOT_RECEIVED.format(task=f"c-{slug}-slack-{uuid.uuid4().hex[:4]}"))

    inv = FailureInvestigation.objects.get()
    assert inv.occurrences == 3 and inv.agents == ["ace", "echo", "eva"]
    assert len(inv.turn_ids) == 3
    assert _debug_turns().count() == 1


def test_a_different_failure_is_a_different_investigation(ada, ws, runner):
    ace = _agent(ws, "ace")
    _fail(ace, runner, NOT_RECEIVED.format(task="c-1"))
    _fail(ace, runner, "emdash create failed: cannot connect to emdash CDP on 127.0.0.1:9223")
    assert FailureInvestigation.objects.count() == 2
    assert _debug_turns().count() == 2


def test_normalization_strips_ids_paths_numbers_and_names():
    a = auto_debug.normalize_note(
        "turn 22662f53-1111-2222-3333-444455556666 failed in /Users/jj/emdash/worktrees/x-y-1 "
        "on haldimagi-mbp-cdp after 3 tries: 'c-turn-4795' (sha 9f8e7d6)",
        known={"haldimagi-mbp-cdp": "<runner>"})
    b = auto_debug.normalize_note(
        "turn 0a1b2c3d-0000-0000-0000-000000000000 failed in /home/ubuntu/w/a-b-2 "
        "on cloud-ec2-1 after 12 tries: 'c-turn-1' (sha 1234abc)",
        known={"cloud-ec2-1": "<runner>"})
    assert a == b
    assert "hasn't" in auto_debug.normalize_note("it hasn't shown up in that session's transcript")


# ---- the skips -----------------------------------------------------------------------


def test_a_requeued_sessionless_failure_does_not_trigger(ada, ws, runner):
    ace = _agent(ws, "ace")
    turn = Turn.objects.create(agent=ace, origin="slack", idempotency_key="sessionless",
                               status=Turn.CLAIMED, claimed_by=runner, claimed_at=timezone.now())
    result = services.finish_turn(turn, status=Turn.FAILED, result_note="emdash create failed: boom")
    assert result.status == Turn.QUEUED
    assert not FailureInvestigation.objects.exists() and not _debug_turns().exists()


def test_a_drill_does_not_trigger(ada, ws, runner):
    ace = _agent(ws, "ace")
    turn = Turn.objects.create(agent=ace, origin="api", idempotency_key="drill",
                               status=Turn.RUNNING, claimed_by=runner, session_key="s-1")
    RunnerDrill.objects.create(runner=runner, agent=ace, turn=turn)
    services.finish_turn(turn, status=Turn.FAILED, result_note="auth expired")
    assert not FailureInvestigation.objects.exists()


@pytest.mark.parametrize("note", [
    "collision on session 'c-ace-1': cancelled by human; will retry",
    "cancelled by user (the agent had already stopped)",
    'You stopped the delivery: the emdash session "c-1" has unsent text sitting in its prompt, …',
])
def test_a_humans_stop_or_collision_does_not_trigger(ada, ws, runner, note):
    _fail(_agent(ws, "ace"), runner, note)
    assert not FailureInvestigation.objects.exists()


@pytest.mark.parametrize("note", [
    "You've hit your session limit · resets 2:30am (America/Denver)",
    "You've hit your limit\n\n[runner] every Claude credential on this box is exhausted "
    "(tried: subscription-1). This runner has paused itself until …",
])
def test_a_usage_cap_does_not_trigger(ada, ws, runner, note):
    """The runner parked itself until the reset; a debugger turn would only spend
    another box's tokens re-reading the cap."""
    _fail(_agent(ws, "ace"), runner, note)
    assert not FailureInvestigation.objects.exists()


def test_a_limit_that_is_not_a_claude_cap_still_triggers(ada, ws, runner):
    _fail(_agent(ws, "ace"), runner, "RecursionError: hit the recursion limit")
    assert FailureInvestigation.objects.exists()


def test_the_kill_switch(ada, ws, runner):
    with override_settings(CANOPY_AUTO_DEBUG=False):
        _fail(_agent(ws, "ace"), runner, "boom")
    assert not FailureInvestigation.objects.exists()


def test_no_debugger_agent_means_nothing_happens(ws, runner):
    _fail(_agent(ws, "ace"), runner, "boom")
    assert not FailureInvestigation.objects.exists() and not _debug_turns().exists()


def test_a_failure_outside_the_debuggers_workspace_tree_is_not_its_business(ada, runner):
    other = a_workspace("elsewhere")
    _fail(_agent(other, "stranger-bot"), runner, "boom")
    assert not FailureInvestigation.objects.exists()


def test_a_failed_debug_turn_never_spawns_another(ada, ws, runner):
    _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    debug = _debug_turns().get()
    Turn.objects.filter(pk=debug.pk).update(status=Turn.RUNNING, claimed_by=runner, session_key="ada-1")
    debug.refresh_from_db()

    services.finish_turn(debug, status=Turn.FAILED, result_note="ada could not start: boom 2")

    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.DEBUGGER_FAILED
    assert _debug_turns().count() == 1
    assert FailureInvestigation.objects.count() == 1   # the debugger's failure is not a new one

    # A recurrence of the same fault only counts — it rides along later, never
    # retriggers on its own.
    _fail(_agent(ws, "ace"), runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.DEBUGGER_FAILED and inv.occurrences == 2
    assert _debug_turns().count() == 1

    # ...and it is reported in the next debug turn some other failure earns.
    _fail(_agent(ws, "ace"), runner, "a completely different failure")
    nxt = _debug_turns().last()
    assert f"investigation {inv.pk} [debug turn failed]" in nxt.prompt
    assert inv.pk in nxt.origin_ref["carries"]
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.OPEN and inv.debug_turn_id == nxt.pk


def test_finish_turn_still_finishes_when_the_hook_raises(ada, ws, runner):
    with mock.patch.object(auto_debug, "_on_turn_failed", side_effect=RuntimeError("bug")):
        result = _fail(_agent(ws, "ace"), runner, "boom")
    assert result.status == Turn.FAILED and result.finished_at is not None


# ---- recurrence after a fix -----------------------------------------------------------


def test_a_failure_that_comes_back_after_resolve_escalates_once(ada, ws, runner):
    ace = _agent(ws, "ace")
    _fail(ace, runner, "boom")
    inv = FailureInvestigation.objects.get()
    auto_debug.resolve(inv, note="restarted emdash")
    _finish_debug_turns()

    _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.ESCALATED
    esc = _debug_turns().last()
    assert esc.idempotency_key == f"auto-debug:{inv.pk}:2"
    assert "ESCALATION" in esc.prompt and "the fix did not hold" in esc.prompt
    assert "restarted emdash" in esc.prompt and esc.origin_ref["escalation"] is True

    # further recurrences only count
    _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.ESCALATED and inv.occurrences == 3
    assert _debug_turns().count() == 2


# ---- the caps ---------------------------------------------------------------------------


@override_settings(CANOPY_AUTO_DEBUG_MAX_PER_HOUR=3, CANOPY_AUTO_DEBUG_MAX_PER_DAY=10)
def test_the_hourly_cap_holds_and_the_next_allowed_turn_carries_the_held(ada, ws, runner):
    ace = _agent(ws, "ace")
    for i in range(5):
        _fail(ace, runner, f"distinct failure kind {'xyz'[i % 3] * (i + 1)}")
    assert _debug_turns().count() == 3
    held = list(FailureInvestigation.objects.filter(status=FailureInvestigation.HELD))
    assert len(held) == 2 and all(h.debug_turn_id is None for h in held)
    assert Event.objects.filter(source="harness.auto_debug", kind="auto_debug.held").count() == 2

    # an hour later the window allows again: the next failure's turn carries them
    an_hour_ago = timezone.now() - dt.timedelta(hours=1, minutes=1)
    _debug_turns().update(created_at=an_hour_ago)
    _fail(ace, runner, "yet another kind of failure")
    nxt = _debug_turns().last()
    assert _debug_turns().count() == 4
    for h in held:
        assert f"investigation {h.pk} [held]" in nxt.prompt
        h.refresh_from_db()
        assert h.status == FailureInvestigation.OPEN and h.debug_turn_id == nxt.pk


@override_settings(CANOPY_AUTO_DEBUG_MAX_PER_HOUR=100, CANOPY_AUTO_DEBUG_MAX_PER_DAY=2)
def test_the_daily_cap(ada, ws, runner):
    ace = _agent(ws, "ace")
    for kind in ("alpha", "beta", "gamma"):
        _fail(ace, runner, f"failure {kind}")
    assert _debug_turns().count() == 2
    assert FailureInvestigation.objects.filter(status=FailureInvestigation.HELD).count() == 1


@override_settings(CANOPY_AUTO_DEBUG_MAX_PER_HOUR=1)
def test_a_held_investigation_retries_on_its_own_recurrence_once_the_cap_allows(ada, ws, runner):
    ace = _agent(ws, "ace")
    _fail(ace, runner, "first kind")
    _fail(ace, runner, "second kind")
    held = FailureInvestigation.objects.get(status=FailureInvestigation.HELD)
    _debug_turns().update(created_at=timezone.now() - dt.timedelta(hours=2))
    _fail(ace, runner, "second kind")
    held.refresh_from_db()
    assert held.status == FailureInvestigation.OPEN and held.debug_turn_id is not None


# ---- lost turns (opt-in) -----------------------------------------------------------------


def _expired(agent, runner):
    return Turn.objects.create(
        agent=agent, origin="slack", idempotency_key=f"lost-{uuid.uuid4()}", status=Turn.RUNNING,
        claimed_by=runner, lease_expires_at=timezone.now() - dt.timedelta(minutes=5),
    )


def test_a_lost_turn_does_not_trigger_by_default(ada, ws, runner):
    _expired(_agent(ws, "ace"), runner)
    services.sweep_expired_leases()
    assert not FailureInvestigation.objects.exists()


def test_a_lost_turn_triggers_when_opted_in(ada, ws, runner):
    _expired(_agent(ws, "ace"), runner)
    with override_settings(CANOPY_AUTO_DEBUG_LOST=True):
        services.sweep_expired_leases()
    inv = FailureInvestigation.objects.get()
    assert "lease expired" in inv.sample_note and _debug_turns().count() == 1


# ---- the API -------------------------------------------------------------------------------


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_the_debugger_lists_and_resolves_through_the_api(ada, ws, runner):
    _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    ada_login = a_user("ada@dimagi-ai.com")
    WorkspaceMembership.objects.create(workspace=ws, user=ada_login, role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=ada.pk).update(user=ada_login)
    c = _client(ada_login)

    listed = c.get("/api/harness/failure-investigations/?status=open").json()
    assert [i["id"] for i in listed["items"]] == [inv.pk]
    assert c.get(f"/api/harness/failure-investigations/{inv.pk}").json()["occurrences"] == 1

    r = c.post(f"/api/harness/failure-investigations/{inv.pk}/resolve",
               data={"note": "fixed the delivery check"}, content_type="application/json")
    assert r.status_code == 200, r.content
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.RESOLVED and inv.resolution_note == "fixed the delivery check"
    assert inv.resolved_by_id == ada_login.pk


def test_an_ordinary_editor_cannot_see_investigations(ada, ws, runner):
    _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    editor = a_user("editor@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    c = _client(editor)
    assert c.get("/api/harness/failure-investigations/").json()["items"] == []
    assert c.get(f"/api/harness/failure-investigations/{inv.pk}").status_code == 404
    assert c.post(f"/api/harness/failure-investigations/{inv.pk}/resolve",
                  data={"note": "x"}, content_type="application/json").status_code == 404
