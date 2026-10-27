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
from apps.attention.models import Alert, AlertEvent
from apps.events.models import Event
from apps.harness import auto_debug, services
from apps.harness import claim
from apps.harness import initiator as who
from apps.harness.models import FailureInvestigation, Runner, RunnerDrill, Turn, TurnEvent
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
                 f"/w/{ws.slug}/agents/ace/turns", "do NOT call get_turn / read_turn_events"):
        assert fact in body, fact
    # 7 of 10 debug turns (2026-10-15..23) opened with get_turn on the failed turn.
    assert f"/api/harness/turns/{failed.pk}" not in body

    # Alert first: the failure IS an alert, held by the debugger — and silent.
    alert = Alert.objects.get(kind="turn_failure", subject_key=f"investigation:{inv.pk}")
    assert alert.status == Alert.OPEN and alert.handler_agent_id == ada.pk and alert.handler_user_id is None
    assert debug.origin_ref["alert"] == alert.pk and f"It is alert {alert.pk}, assigned to you" in body
    assert not alert.events.filter(kind=AlertEvent.NOTIFIED).exists()


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


def test_each_undelivered_cause_is_its_own_investigation():
    """The runner's "Couldn't confirm…" note now ends with WHY recovery gave up
    (execute._NOT_RECEIVED_CAUSES). Investigation 2 merged four causes under one
    fingerprint (2026-10-14 → 10-23); the cause sentence must survive normalization."""
    base = ("Couldn't confirm your message was delivered. It was typed into the emdash "
            "session \"c-can-you-share-live-urls-to-the-3218\", but it hasn't shown up in "
            "that session's transcript, so it may or may not have arrived. Check the session "
            "before sending it again, so it doesn't land twice.")
    covered = base + (" When the runner looked again, the session was showing something "
                      "other than its input box (a menu, a dialog or a scrolled view), so it "
                      "left it alone.")
    exhausted = base + " The runner sent it again and it still did not show up."
    fps = {auto_debug.fingerprint("failed", auto_debug.normalize_note(n))
           for n in (base, covered, exhausted)}
    assert len(fps) == 3


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


def _alert(inv):
    return Alert.objects.get(kind="turn_failure", subject_key=f"investigation:{inv.pk}")


def _notified(inv) -> list[list[int]]:
    """Who each `notified` event on the investigation's alert reached."""
    return [e.payload["to"] for e in _alert(inv).events.filter(kind=AlertEvent.NOTIFIED)]


FIX = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"


def test_a_clean_fix_closes_the_alert_and_tells_nobody(ada, ws, runner, django_capture_on_commit_callbacks):
    """Jonathan, 2026-10-23: "in a clean alert -> ada fix -> resolved, I don't
    want to be notified at all". Failure (b)."""
    with django_capture_on_commit_callbacks(execute=True):
        _fail(_agent(ws, "ace"), runner, "boom")
        inv = FailureInvestigation.objects.get()
        auto_debug.resolve(inv, note="the probe misread a usage cap", pr="https://github.com/x/y/pull/1",
                           merged=True)
    alert = _alert(inv)
    assert alert.status == Alert.RESOLVED and _notified(inv) == []
    resolved = alert.events.get(kind=AlertEvent.RESOLVED)
    assert resolved.payload["cause"] == "the probe misread a usage cap"
    assert resolved.payload["pr"] == "https://github.com/x/y/pull/1" and resolved.payload["merged"] is True
    assert resolved.payload["deploy"] == "no runner fix named"


def test_a_failure_that_comes_back_after_resolve_goes_to_a_person_with_no_turn(
        ada, ws, runner, django_capture_on_commit_callbacks):
    """Investigation 10 (2026-10-22): resolved, came back 47 seconds later, and a
    second debug turn ran two minutes after the first. Failure (a)."""
    ace = _agent(ws, "ace")
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
        inv = FailureInvestigation.objects.get()
        auto_debug.resolve(inv, note="restarted emdash")
        _finish_debug_turns()
        _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.ESCALATED and inv.handler_id == ada.owner_id
    assert "restarted emdash" in inv.ask
    assert _debug_turns().count() == 1
    alert = _alert(inv)
    assert alert.status == Alert.OPEN and alert.handler_user_id == ada.owner_id and alert.handler_agent_id is None
    assert _notified(inv) == [[ada.owner_id]]

    # further recurrences only count — no turn, no second notification
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.ESCALATED and inv.occurrences == 3
    assert _debug_turns().count() == 1 and _notified(inv) == [[ada.owner_id]]


def _code(runner, sha, committed):
    Runner.objects.filter(pk=runner.pk).update(code_sha=sha, code_committed_at=int(committed.timestamp()))
    runner.refresh_from_db()


def test_a_recurrence_on_a_runner_without_the_fix_only_counts(ada, ws, runner, django_capture_on_commit_callbacks):
    """#1488, #1504, #1527 and #1965 were resolved "merged, NOT yet deployed", and
    investigation 2's 10-23 recurrence on runner code without the fix was counted
    as "the fix did not hold". Failure (c)."""
    fixed_at = timezone.now() - dt.timedelta(hours=1)
    _code(runner, "1111111aaaa", fixed_at - dt.timedelta(days=2))   # older than the fix
    ace = _agent(ws, "ace")
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
        inv = FailureInvestigation.objects.get()
        auto_debug.resolve(inv, note="handle the dialog", fix_commit=FIX, fix_committed_at=fixed_at,
                           pr="#1965", merged=True)
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.PENDING_DEPLOY
    alert = _alert(inv)
    assert alert.status == Alert.OPEN and alert.handler_agent_id == ada.pk   # quiet, still the agent's
    assert alert.title.startswith("Fix pending deploy on haldimagi-mbp-cdp")

    _finish_debug_turns()
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.PENDING_DEPLOY and inv.occurrences == 2
    assert _debug_turns().count() == 1 and _notified(inv) == []

    # The box updates past the fix: the alert closes on the tick, by itself, silently.
    _code(runner, "2222222bbbb", fixed_at + dt.timedelta(minutes=5))
    from apps.harness import alerts_turn_failure

    alerts_turn_failure.reconcile(timezone.now())
    inv.refresh_from_db()
    alert.refresh_from_db()
    assert inv.status == FailureInvestigation.RESOLVED and alert.status == Alert.RESOLVED
    assert alert.events.get(kind=AlertEvent.RESOLVED).payload["deploy"] == "live on every runner it hit"
    assert _notified(inv) == []


def test_a_recurrence_on_a_runner_that_runs_the_fix_reopens_it_for_a_person(
        ada, ws, runner, django_capture_on_commit_callbacks):
    fixed_at = timezone.now() - dt.timedelta(hours=1)
    _code(runner, FIX, fixed_at)
    ace = _agent(ws, "ace")
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
        inv = FailureInvestigation.objects.get()
        auto_debug.resolve(inv, note="handle the dialog", fix_commit=FIX[:10], fix_committed_at=fixed_at)
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.RESOLVED and _alert(inv).status == Alert.RESOLVED

    _finish_debug_turns()
    with django_capture_on_commit_callbacks(execute=True):
        _fail(ace, runner, "boom")
    inv.refresh_from_db()
    alert = _alert(inv)
    assert inv.status == FailureInvestigation.ESCALATED and "the fix did not hold" in inv.ask
    assert alert.status == Alert.OPEN and alert.handler_user_id == ada.owner_id   # the SAME alert, reopened
    assert Alert.objects.filter(kind="turn_failure").count() == 1
    assert _debug_turns().count() == 1 and _notified(inv) == [[ada.owner_id]]


def test_the_debugger_hands_off_and_can_no_longer_resolve(ada, ws, runner, django_capture_on_commit_callbacks):
    ada_login = a_user("ada@dimagi-ai.com")
    WorkspaceMembership.objects.create(workspace=ws, user=ada_login, role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=ada.pk).update(user=ada_login)
    with django_capture_on_commit_callbacks(execute=True):
        _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    c = _client(ada_login)
    base = f"/api/harness/failure-investigations/{inv.pk}"

    with django_capture_on_commit_callbacks(execute=True):
        r = c.post(f"{base}/hand-off", data={"ask": "Sign Claude back in on cloud-ec2-2."},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "escalated" and r.json()["ask"] == "Sign Claude back in on cloud-ec2-2."
    alert = _alert(inv)
    assert alert.status == Alert.OPEN and alert.handler_user_id == ada.owner_id
    assigned = alert.events.filter(kind=AlertEvent.ASSIGNED).order_by("at", "pk")
    assert [e.payload["agent"] for e in assigned] == ["ada", None]
    assert assigned.last().payload["ask"] == "Sign Claude back in on cloud-ec2-2."
    assert _notified(inv) == [[ada.owner_id]]

    # The escalation loop: resolve was the debugger's only verb, and resolving
    # "needs Jonathan" cleared the alert and re-armed the next escalation.
    r = c.post(f"{base}/resolve", data={"note": "handed off"}, content_type="application/json")
    assert r.status_code == 409, r.content
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.ESCALATED and _alert(inv).status == Alert.OPEN

    # A person may close it.
    r = _client(ada.owner).post(f"{base}/resolve", data={"note": "signed back in"},
                                content_type="application/json")
    assert r.status_code == 200, r.content
    assert _alert(inv).status == Alert.RESOLVED


def test_a_hand_off_to_someone_who_cannot_handle_it_is_refused(ada, ws, runner):
    _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    editor = a_user("editor@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    r = _client(ada.owner).post(f"/api/harness/failure-investigations/{inv.pk}/hand-off",
                                data={"ask": "look", "to": "editor@dimagi.com"},
                                content_type="application/json")
    assert r.status_code == 400, r.content
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.OPEN


def test_no_two_debug_turns_for_one_failure_inside_30_minutes(ada, ws, runner):
    """A held failure that already had a debug turn minutes ago waits it out."""
    ace = _agent(ws, "ace")
    _fail(ace, runner, "boom")
    inv = FailureInvestigation.objects.get()
    FailureInvestigation.objects.filter(pk=inv.pk).update(status=FailureInvestigation.HELD)
    _finish_debug_turns()
    _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.HELD and _debug_turns().count() == 1

    _debug_turns().update(created_at=timezone.now() - dt.timedelta(minutes=31))
    _fail(ace, runner, "boom")
    inv.refresh_from_db()
    assert inv.status == FailureInvestigation.OPEN and _debug_turns().count() == 2


def test_the_prompt_carries_the_runner_state_but_nothing_a_person_typed(ada, ws, runner):
    """Runner state decided 5 of 6 non-delivery causes (2026-10-15..23): a signed-out
    login probe on cloud-ec2-2 (investigations 5, 10), a usage cap read as auth (9),
    the code sha for every deploy question. None of it was in the prompt."""
    from apps.harness.models import RunnerLogin

    Runner.objects.filter(pk=runner.pk).update(
        code_sha="abc1234def", ready=False, ready_note="Not logged in · Please run /login",
        paused=True, paused_note="jonathan's private reason", unpause_at=timezone.now() + dt.timedelta(hours=2))
    RunnerLogin.objects.create(runner=runner, key="default", kind=RunnerLogin.LOGIN, signed_in=False,
                               account="someone@dimagi.com", reported_at=timezone.now())
    with override_settings(RUNNER_CODE_SHA="fff9999eee"):
        _fail(_agent(ws, "ace"), runner, "boom")
    prompt = _debug_turns().get().prompt
    for fact in ("The failing runner", "ready=False (Not logged in · Please run /login)",
                 "paused: True until", "Code: abc1234def (expected fff9999eee; current: NO",
                 "Claude login default (login): signed_in=False"):
        assert fact in prompt, fact
    assert "private reason" not in prompt and "someone@dimagi.com" not in prompt
    snap = FailureInvestigation.objects.get().evidence
    assert snap["code_current"] is False and snap["logins"][0]["signed_in"] is False


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


@override_settings(CANOPY_AUTO_DEBUG_MAX_PER_HOUR=2, CANOPY_AUTO_DEBUG_MAX_PER_DAY=10)
def test_turns_another_workspace_keys_like_debug_turns_do_not_spend_the_cap(ada, ws, runner):
    # Keys are unique per workspace and whoever posts a turn picks its key, so
    # `auto-debug:` is a guessable prefix, not the mark of a debug turn. A member
    # of an unrelated workspace posting turns under it must not exhaust the
    # debugger's cap and so silence the fleet's failure handling.
    other = a_workspace("elsewhere")
    mallory = a_member(other)
    bot = _agent(other, "mallory-bot")
    for i in range(5):
        services.enqueue_turn(
            agent=bot, origin=Turn.ORIGIN_API, idempotency_key=f"{auto_debug.KEY_PREFIX}{i}:1",
            prompt="hi", origin_ref={"auto_debug": i},
            initiator=who.for_user(mallory, via="api", assurance=who.PAT),
        )
    assert auto_debug.cap_blocks(timezone.now()) == ""

    _fail(_agent(ws, "ace"), runner, "boom")
    inv = FailureInvestigation.objects.get()
    assert inv.status == FailureInvestigation.OPEN and inv.debug_turn is not None


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
    claim.sweep_expired_leases()
    assert not FailureInvestigation.objects.exists()


def test_a_lost_turn_triggers_when_opted_in(ada, ws, runner):
    _expired(_agent(ws, "ace"), runner)
    with override_settings(CANOPY_AUTO_DEBUG_LOST=True):
        claim.sweep_expired_leases()
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


def test_a_turn_that_must_stay_on_sensitive_runners_is_never_re_processed(ada, ws, runner):
    # Its note can quote what the turn read, and the debugger runs wherever ada does
    # (spec 2026-10-22-sensitive-data-agents §4).
    from apps.canopy_sessions.models import Session

    ace = _agent(ws, "ace")
    s = Session.objects.create(agent=ace, workspace=ws, title="chat",
                               metadata={"runner_requirements": ["sensitive_data"]})
    n = next(_seq)
    turn = Turn.objects.create(
        origin="canopy_web_chat", idempotency_key=f"t-{n}-{uuid.uuid4()}",
        status=Turn.RUNNING, claimed_by=runner, claimed_at=timezone.now(),
        session_key=f"c-ace-{n}", chat_session=s,
    )
    services.finish_turn(turn, status=Turn.FAILED, result_note=NOT_RECEIVED.format(task="x"))

    assert not FailureInvestigation.objects.exists()
    assert not _debug_turns().exists()


def test_the_prompt_carries_the_delivery_evidence_but_not_the_words(ada, ws, runner):
    """Investigation 2: the debugger got 404 on the failed chat turns (the chat ACL),
    so it could not see which recovery path the runner took. canopy copies the
    diagnostic fields into the prompt; what the composer and screen showed stays
    behind the ACL."""
    ace = _agent(ws, "ace")
    turn = Turn.objects.create(agent=ace, origin="slack", idempotency_key="evidence",
                               status=Turn.RUNNING, claimed_by=runner, session_key="s-9")
    for seq, payload in enumerate([
        {"status": "delivery_snapshot", "task": "s-9", "found": True, "running": False,
         "line": "my private words"},
        {"status": "delivery_retry", "task": "s-9", "attempt": 1, "action": "leave",
         "reason": "composer_not_visible"},
        {"status": "delivery_screen", "task": "s-9", "attempt": 1,
         "screen_tail": "a secret on screen"},
        {"status": "undelivered", "task": "s-9", "reason": "composer_not_visible", "landed": "no"},
        {"status": "reused_session", "task": "s-9"},
    ], start=1):
        TurnEvent.objects.create(turn=turn, seq=seq, kind="status", payload=payload)
    prompt = auto_debug.render_prompt(
        FailureInvestigation(pk=7, occurrences=1, first_seen=timezone.now(),
                             last_seen=timezone.now()), turn)
    assert "status=delivery_snapshot found=True running=False" in prompt
    assert "reason=composer_not_visible" in prompt
    assert "withheld here: line" in prompt and "withheld here: screen_tail" in prompt
    assert "my private words" not in prompt and "a secret on screen" not in prompt
    assert "reused_session" not in prompt
    # The runner's own verdict, so the debugger never needs the transcript.
    assert "landed: no" in prompt and auto_debug.landed(turn) == "no"


def test_the_prompt_carries_a_failed_reuse_sends_cdp_error(ada, ws, runner):
    """Investigation 15 (turn c3717cce, 2026-10-27): "reuse failed on existing session
    … not spawning a duplicate" names no error; the runner records which one only on
    its `kind="error"` reuse_send_failed event, which the prompt used to leave out."""
    ace = _agent(ws, "ace")
    turn = Turn.objects.create(agent=ace, origin="email", idempotency_key="reuse-err",
                               status=Turn.RUNNING, claimed_by=runner, session_key="s-10")
    TurnEvent.objects.create(turn=turn, seq=1, kind="error", payload={
        "status": "reuse_send_failed", "task": "s-10", "task_state": "live",
        "detail": "COMPOSER_NOT_VISIBLE: no input line in the rendered frame"})
    TurnEvent.objects.create(turn=turn, seq=2, kind="error", payload={
        "status": "runner_crashed", "detail": "unrelated"})
    prompt = auto_debug.render_prompt(
        FailureInvestigation(pk=8, occurrences=1, first_seen=timezone.now(),
                             last_seen=timezone.now()), turn)
    assert "status=reuse_send_failed detail=COMPOSER_NOT_VISIBLE" in prompt
    assert "runner_crashed" not in prompt
