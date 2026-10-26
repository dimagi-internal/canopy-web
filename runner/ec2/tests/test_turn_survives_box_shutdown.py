"""A turn killed because its box is powering off (a fleet member terminated, an
instance stopped) is not reported failed: it is left running, so canopy-web queues
it again for another box (`fleets.requeue_lost`). A restart of the runner alone (an
update) and a turn that really failed are reported as before."""
from __future__ import annotations


def _finishes(mod, monkeypatch, *, ok: bool, stopping: bool, shutting_down: bool) -> list:
    sent = []
    monkeypatch.setattr(mod, "_stop", stopping)
    monkeypatch.setattr(mod, "_system_shutting_down", lambda: shutting_down)
    monkeypatch.setattr(mod, "_ws_request", lambda ws, frame, want: sent.append(frame) or {"type": want})
    mod._finish_turn(object(), "t-1234567890", ok, "killed", {"id": "t-1234567890"})
    return sent


def test_a_turn_killed_by_the_box_powering_off_is_left_to_be_queued_again(load_cloud_runner, monkeypatch):
    mod = load_cloud_runner()
    assert _finishes(mod, monkeypatch, ok=False, stopping=True, shutting_down=True) == []
    # Its Claude process can die of the shutdown before the runner's own SIGTERM lands.
    assert _finishes(mod, monkeypatch, ok=False, stopping=False, shutting_down=True) == []


def test_the_worker_does_not_report_it_either(load_cloud_runner, monkeypatch):
    """The worker finishes over REST (`_run_turn_worker`), not through `_finish_turn`."""
    mod = load_cloud_runner()
    monkeypatch.setattr(mod, "_system_shutting_down", lambda: True)
    assert mod._killed_by_shutdown(False, "t-1234567890") is True
    assert mod._killed_by_shutdown(True, "t-1234567890") is False
    monkeypatch.setattr(mod, "_system_shutting_down", lambda: False)
    assert mod._killed_by_shutdown(False, "t-1234567890") is False


def test_a_runner_restart_or_a_real_failure_is_still_reported(load_cloud_runner, monkeypatch):
    mod = load_cloud_runner()
    assert [f["status"] for f in _finishes(mod, monkeypatch, ok=False, stopping=True, shutting_down=False)] == ["failed"]
    assert [f["status"] for f in _finishes(mod, monkeypatch, ok=True, stopping=True, shutting_down=True)] == ["done"]
