"""Stopping a session that no Turn owns.

The turn-shaped cancel can only reach chat: an agent/board/scheduled turn is
fire-and-continue and terminal seconds after its prompt is delivered, so those
sessions were unstoppable from the web no matter how hard anyone pressed the button.
"""
import types

import pytest

from canopy_runner import session_interrupt


class _FakeClient:
    def __init__(self):
        self.streamed = []

    def post_session_stream(self, runner_id, session_id, events, transcript_id=""):
        self.streamed.append((session_id, events))


def _cfg():
    return types.SimpleNamespace(cdp_port=9222, runner_id="r1")


@pytest.fixture(autouse=True)
def _clean():
    session_interrupt._pending.clear()
    yield
    session_interrupt._pending.clear()


def _interrupts(monkeypatch, *actions):
    from canopy_runner import cdp_control

    calls = []
    seq = list(actions)

    def _fake(task, port=None):
        calls.append(task)
        action = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(action, Exception):
            raise action
        return {"ok": True, "action": action}

    monkeypatch.setattr(cdp_control, "interrupt", _fake)
    return calls


def test_a_rung_session_is_interrupted_and_reported_idle(monkeypatch):
    calls = _interrupts(monkeypatch, "interrupted")
    client = _FakeClient()
    session_interrupt.ring("hal-canopy-sweep", "sess-1")

    assert session_interrupt.drain(_cfg(), client, "r1") == 1

    assert calls == ["hal-canopy-sweep"]
    assert session_interrupt._pending == {}, "a landed stop leaves no state behind"
    # The web has only `activity` to go on, and after a confirmed interrupt idle is
    # simply true — without it the next session report re-asserts `working` and the
    # stop appears to undo itself.
    [(session_id, events)] = client.streamed
    assert session_id == "sess-1"
    kinds = [e["kind"] for e in events]
    # TWO independent facts: what the agent is doing, and whether the stop took.
    assert "activity:idle" in kinds
    assert "stop:stopped" in kinds


def test_an_unconfirmed_stop_retries_then_gives_up_quietly(monkeypatch):
    """Never reported idle on a stop we could not confirm: the session stays
    `working`, which is also true, and is what tells the human it did not take."""
    calls = _interrupts(monkeypatch, "still-running")
    client = _FakeClient()
    session_interrupt.ring("hal-canopy-sweep", "sess-1")

    for _ in range(session_interrupt.MAX_ATTEMPTS - 1):
        assert session_interrupt.drain(_cfg(), client, "r1") == 0
        assert "hal-canopy-sweep" in session_interrupt._pending, "keep trying"

    assert session_interrupt.drain(_cfg(), client, "r1") == 0
    assert len(calls) == session_interrupt.MAX_ATTEMPTS
    assert session_interrupt._pending == {}, "bounded — never retried forever"

    # It must SAY SO. Reporting only the success left a failed stop
    # indistinguishable from a stop nobody asked for: the agent just went on
    # working with no explanation, which is how a dead Stop button stayed
    # invisible in the first place.
    [(session_id, events)] = client.streamed
    kinds = [e["kind"] for e in events]
    assert kinds == ["stop:failed"]
    assert "activity:idle" not in kinds, "it is still working — do not claim idle"
    assert events[0]["payload"]["attempts"] == session_interrupt.MAX_ATTEMPTS


def test_an_old_sidecar_counts_as_unconfirmed_not_success(monkeypatch):
    """Runner and sidecar update separately, so a sidecar with no `action` WILL run
    under this code. Reading a missing key as success is the false green again."""
    from canopy_runner import cdp_control

    monkeypatch.setattr(cdp_control, "interrupt", lambda task, port=None: {"ok": True})
    client = _FakeClient()
    session_interrupt.ring("hal-canopy-sweep", "sess-1")

    assert session_interrupt.drain(_cfg(), client, "r1") == 0
    assert client.streamed == [], "one unconfirmed attempt is not yet a verdict"


def test_a_dead_emdash_never_kills_the_loop(monkeypatch):
    _interrupts(monkeypatch, RuntimeError("emdash gone"))
    client = _FakeClient()
    session_interrupt.ring("hal-canopy-sweep", "sess-1")

    assert session_interrupt.drain(_cfg(), client, "r1") == 0  # no raise


def test_jabbing_the_button_does_not_multiply_the_escapes(monkeypatch):
    """Three impatient presses are one stop, not nine Escapes at a terminal that
    may since have moved on to other work."""
    calls = _interrupts(monkeypatch, "still-running")
    client = _FakeClient()
    for _ in range(3):
        session_interrupt.ring("hal-canopy-sweep", "sess-1")

    session_interrupt.drain(_cfg(), client, "r1")

    assert len(calls) == 1, "one tick, one Escape, however many times it was rung"


def test_a_ring_with_no_session_key_is_ignored():
    session_interrupt.ring("", "sess-1")
    assert session_interrupt._pending == {}
