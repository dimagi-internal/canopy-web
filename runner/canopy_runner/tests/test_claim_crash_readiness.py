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
