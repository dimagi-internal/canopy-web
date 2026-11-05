"""Runners racing the claim, and closers racing each other, against real row locks.

The suite runs on SQLite, which has no row locks and serializes every writer on one
file lock, so the interleavings that matter here cannot happen there. Each test
below starts its racers together behind a barrier, on their own connections, and
checks the invariant the locking exists for:

* the claim (`claim.claim_next_turn`): every queued turn is claimed by exactly one
  runner, and one call hands out at most one turn — the conditional UPDATE's
  `status=QUEUED` re-check, which PostgreSQL re-evaluates after the competing
  writer's row lock is released;
* one conversation never executes two turns at once (`one_executing_turn_per_session`
  and the claim's IntegrityError retry);
* the close (`turn_close.close_turn`): a duplicate finish, or the lease sweep racing
  a finish, leaves exactly one terminal state and fires its side effects (the
  ledger's status line, the `turn.finished` analytics fact) exactly once;
* the ledger (`ledger.append_events`): concurrent appenders to one turn serialize on
  its `SELECT … FOR UPDATE`, so no append dies on the per-turn seq index.

Skipped on SQLite; CI's `Backend pytest (PostgreSQL)` job runs it.
"""
from __future__ import annotations

import datetime as dt
import threading
from collections import Counter

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.events.models import Activity
from apps.harness import claim, runner_protocol
from apps.harness.ledger import append_events
from apps.harness.models import Runner, Turn, TurnEvent
from apps.harness.schemas.turns import TurnFinishIn
from apps.harness.testing import enrolled
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL"),
    pytest.mark.django_db(transaction=True),
]

RUNNERS = 6
TURNS = 12


def _race(fns, timeout=60):
    """Run each of `fns` in its own thread, released together by a barrier; return
    their results in order. A racer's exception is re-raised here, so a lock that
    fails as an IntegrityError fails the test rather than vanishing in a thread."""
    barrier = threading.Barrier(len(fns))
    results: list = [None] * len(fns)
    failures: list[BaseException] = []

    def run(i, fn):
        try:
            barrier.wait(timeout)
            results[i] = fn()
        except BaseException as e:  # noqa: BLE001 — carried back to the test's own thread
            failures.append(e)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a racer hung"
    assert not failures, failures
    return results


@pytest.fixture
def owner():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _runners(owner, n=RUNNERS):
    """Session-capable cloud boxes of one owner: every one is eligible for every
    session turn below, so they all compete for the same rows."""
    return [enrolled(Runner.objects.create(
        name=f"cloud-{i}", kind=Runner.CLOUD, host="", owner=owner, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={"sessions": True},
    )) for i in range(n)]


def _turn(ws, owner, key, session=None, **kw):
    session = session or Session.objects.create(workspace=ws, created_by=owner)
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=key, enqueued_by=owner, prompt="hi", **kw)


def _claimed_turn(ws, owner, runner, key, **kw):
    turn = _turn(ws, owner, key)
    got = claim.claim_next_turn(runner)
    assert got is not None and got.pk == turn.pk
    if kw:
        Turn.objects.filter(pk=turn.pk).update(**kw)
    return Turn.objects.get(pk=turn.pk)


def _status_lines(turn, status):
    return sum(1 for p in TurnEvent.objects.filter(turn=turn, kind="status").values_list("payload", flat=True)
               if (p or {}).get("status") == status)


def _terminal_lines(turn):
    return sum(_status_lines(turn, s) for s in Turn.TERMINAL)


def test_racing_runners_claim_every_turn_exactly_once(owner, workspace):
    runners = _runners(owner)
    turns = [_turn(workspace, owner, f"t{i}") for i in range(TURNS)]

    def drain(runner):
        got = []
        while (turn := claim.claim_next_turn(runner)) is not None:
            assert isinstance(turn, Turn)  # one call, at most one turn
            got.append(turn.pk)
        return got

    claims = _race([lambda r=r: drain(r) for r in runners])

    by_turn = Counter(pk for got in claims for pk in got)
    assert {pk: n for pk, n in by_turn.items() if n > 1} == {}, "a turn was handed to two runners"
    assert set(by_turn) == {t.pk for t in turns}, "a claimable turn was left behind"
    for runner, got in zip(runners, claims):
        for pk in got:
            turn = Turn.objects.get(pk=pk)
            # The row agrees with the runner that was told it won.
            assert (turn.status, turn.claimed_by_id) == (Turn.CLAIMED, runner.pk)
            assert _status_lines(turn, Turn.CLAIMED) == 1


def test_one_conversation_never_executes_two_turns_at_once(owner, workspace):
    runners = _runners(owner)
    session = Session.objects.create(workspace=workspace, created_by=owner)
    for i in range(3):
        _turn(workspace, owner, f"s{i}", session=session)

    results = _race([lambda r=r: claim.claim_next_turn(r) for r in runners])

    executing = Turn.objects.filter(chat_session=session, rides_turn__isnull=True,
                                    status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN])
    assert executing.count() == 1
    assert sum(1 for t in results if t is not None and t.rides_turn_id is None) == 1


def _finished_facts(turn):
    return Activity.objects.filter(kind="turn.finished", ref=str(turn.pk)).count()


def test_a_duplicate_finish_closes_the_turn_once(owner, workspace):
    runner = _runners(owner, 1)[0]
    turns = [_claimed_turn(workspace, owner, runner, f"f{i}", status=Turn.RUNNING) for i in range(4)]

    def finish(pk):
        return runner_protocol.finish(Turn.objects.get(pk=pk), TurnFinishIn(status="done", result_note="ok"))

    # Four reports of each turn's finish (a runner's REST retry crossing its socket
    # frame), all released together.
    _race([lambda pk=t.pk: finish(pk) for t in turns for _ in range(4)])

    for turn in turns:
        turn.refresh_from_db()
        assert turn.status == Turn.DONE
        assert _terminal_lines(turn) == 1
        assert _finished_facts(turn) == 1


def test_the_lease_sweep_racing_a_finish_leaves_one_terminal_state(owner, workspace):
    runner = _runners(owner, 1)[0]
    past = timezone.now() - dt.timedelta(minutes=5)
    turns = [_claimed_turn(workspace, owner, runner, f"l{i}", status=Turn.RUNNING, lease_expires_at=past)
             for i in range(6)]

    def finish(pk):
        return runner_protocol.finish(Turn.objects.get(pk=pk), TurnFinishIn(status="done"))

    # The finish arrives just as its lease ran out: the sweep and the runner's report
    # race for every turn (two sweeps, as two web processes' clocks would).
    _race([claim.sweep_expired_leases, claim.sweep_expired_leases]
          + [lambda pk=t.pk: finish(pk) for t in turns])

    for turn in turns:
        turn.refresh_from_db()
        assert turn.status in (Turn.DONE, Turn.LOST)
        assert _terminal_lines(turn) == 1, "both closers fired their side effects"
        assert _finished_facts(turn) == 1


def test_concurrent_appends_to_one_turn_never_collide_on_seq(owner, workspace):
    runner = _runners(owner, 1)[0]
    turn = _claimed_turn(workspace, owner, runner, "a0")
    before = TurnEvent.objects.filter(turn=turn).count()

    def append(i):
        for j in range(5):
            append_events(Turn.objects.get(pk=turn.pk),
                          [{"kind": "assistant", "payload": {"text": f"{i}.{j}"}}])

    _race([lambda i=i: append(i) for i in range(8)])

    seqs = list(TurnEvent.objects.filter(turn=turn).order_by("seq").values_list("seq", flat=True))
    assert len(seqs) == before + 40
    assert seqs == list(range(1, len(seqs) + 1))
