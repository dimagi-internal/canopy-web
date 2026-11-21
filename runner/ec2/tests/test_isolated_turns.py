"""A learning run's turn keeps nothing on the box for the agent's later turns (canopy-web `harness.isolation`).

canopy-web marks such a turn `isolated` and hands it only to a box that reports `isolated` on its heartbeat; the
box turns Claude Code's auto memory off for it, so nothing it read reaches the memory later turns load."""
from __future__ import annotations


def test_the_heartbeat_says_this_code_isolates(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    assert cloud_runner._heartbeat_body([])["isolated"] == cloud_runner.ISOLATED_VERSION == 1


def test_an_isolated_turn_runs_without_auto_memory_and_any_other_turn_as_before(cloud_runner):
    assert cloud_runner._isolation_env({"id": "t", "isolated": True}) == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    assert cloud_runner._isolation_env({"id": "t", "isolated": False}) == {}
    assert cloud_runner._isolation_env({"id": "t"}) == {}             # an older canopy-web: unchanged
