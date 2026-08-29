"""Task 2 completeness gap: _claim_and_execute's crash path (execute_turn raising an
uncaught exception) must mark the runner not-ready, same as execute.py's own fail sites,
so the runner stops advertising ready=True and stops re-claiming into a repeat crash.
"""
from types import SimpleNamespace

from canopy_runner import execute, main as main_mod, readiness


class FakeClient:
    def __init__(self, turn):
        self.turn = turn
        self.failed = []

    def claim(self, runner_id, paused_agents=None):
        return self.turn

    def fail_turn(self, turn_id, note):
        self.failed.append((turn_id, note))


def _cfg(tmp_path):
    # A throwaway tmp state_path — readiness.mark_failed writes its marker next to it.
    # Never touches the real ~/.canopy.
    return SimpleNamespace(runner_id="r-1", state_path=str(tmp_path / "runner-state.json"))


def test_execute_crash_marks_runner_not_ready(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(execute, "execute_turn", _boom)

    cfg = _cfg(tmp_path)
    client = FakeClient({"id": "t-1", "agent_slug": "echo"})

    result = main_mod._claim_and_execute(cfg, client, paused=set())

    # existing crash-path behavior: turn fails server-side, loop survives
    assert result == "failed:t-1"
    assert client.failed and client.failed[0][0] == "t-1"
    assert "runner execute crashed" in client.failed[0][1]

    # the gap this test closes: the reactive readiness marker must also be written,
    # so a subsequent readiness.compute() reports not-ready instead of silently
    # keeping ready=True and re-claiming into another crash.
    marker = readiness._marker(cfg)
    assert marker.exists(), "execute_turn crash must call readiness.mark_failed"
    assert "runner execute crashed" in marker.read_text()


def test_a_cancel_arriving_mid_execute_survives_for_the_pump(tmp_path, monkeypatch):
    """The stop that vanished.

    `cancel` lands on the WAKE-LISTENER thread while this one is still inside
    execute_turn — a window that includes the CDP send and `_wait_for_transcript`
    (up to 45s for a fresh session). execute_chat_turn hands off without consulting
    the set, so the only consumer is the pump, on a LATER tick — and the `finally`
    here used to wipe the id before that tick ever ran. The stop was dropped in
    silence, the agent ran the whole turn, and the server's DONE -> CANCELLED
    backstop then labelled the completed turn "cancelled".
    """
    from canopy_runner import chat_bridge
    from canopy_runner.cancel import CANCELLED_TURNS

    CANCELLED_TURNS.clear()
    chat_bridge.IN_FLIGHT.clear()

    def _register_bridge_then_cancel(cfg, client, runner_id, turn, cancel_check=None):
        # What execute_chat_turn does on the happy path...
        chat_bridge.IN_FLIGHT[turn["id"]] = object()
        # ...and the human pressing stop on the other thread, before we return.
        CANCELLED_TURNS.add(turn["id"])
        return f"chat:{turn['id']}:echo-1"

    monkeypatch.setattr(execute, "execute_turn", _register_bridge_then_cancel)
    try:
        main_mod._claim_and_execute(_cfg(tmp_path), FakeClient({"id": "t-1", "agent_slug": "echo"}),
                                    paused=set())
        assert "t-1" in CANCELLED_TURNS, "the pump owns this turn now — don't drop its stop"
    finally:
        CANCELLED_TURNS.clear()
        chat_bridge.IN_FLIGHT.clear()


def test_a_cancel_with_no_bridge_is_still_evicted(tmp_path, monkeypatch):
    """The eviction the guard must not break: a non-chat turn registers no bridge, so
    its id has no future consumer and leaving it in would grow the set forever (and
    latently mark any turn that reused the id)."""
    from canopy_runner import chat_bridge
    from canopy_runner.cancel import CANCELLED_TURNS

    CANCELLED_TURNS.clear()
    chat_bridge.IN_FLIGHT.clear()

    def _no_bridge(cfg, client, runner_id, turn, cancel_check=None):
        CANCELLED_TURNS.add(turn["id"])
        return f"reused:{turn['id']}"

    monkeypatch.setattr(execute, "execute_turn", _no_bridge)
    try:
        main_mod._claim_and_execute(_cfg(tmp_path), FakeClient({"id": "t-1", "agent_slug": "echo"}),
                                    paused=set())
        assert "t-1" not in CANCELLED_TURNS
    finally:
        CANCELLED_TURNS.clear()
