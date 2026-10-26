"""A turn killed because its box is powering off (a fleet member terminated, an
instance stopped) is not reported failed: it is left running, so canopy-web queues
it again for another box (`fleets.requeue_lost`). A restart of the runner alone (an
update), a person's stop and a turn that really failed are reported as before.

#2148 put this guard in `_finish_turn`, which nothing called: the worker finishes
over REST in `_run_turn`. Turn 2087c077 (canopy-web on demo-0fc9c7b1efb87c5c3,
2026-10-26, failure investigation 13) ran #2148 and was still reported failed with
"turn ended: cancelled". These tests drive `_run_turn` itself.
"""
from __future__ import annotations

import threading

import pytest

TID = "2087c077-7482-473f-bc17-5adc3c2fefcb"


@pytest.fixture
def run(cloud_runner, monkeypatch, tmp_path):
    mod = cloud_runner
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: threading.Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a, **k: None)
    timeline = []
    monkeypatch.setattr(mod, "_api",
                        lambda m, path, body=None, **k: timeline.append((path, body)) or (200, {}))
    with mod._TURNS_LOCK:
        mod._IN_FLIGHT[TID] = None
    yield mod, timeline
    with mod._TURNS_LOCK:
        mod._IN_FLIGHT.pop(TID, None)
    mod._clear_stop(TID)


def _finishes(run, monkeypatch, *, ok: bool, stopping: bool, shutting_down: bool,
              person_stop: bool = False) -> list:
    mod, timeline = run
    monkeypatch.setattr(mod, "_stop", stopping)
    monkeypatch.setattr(mod, "_system_shutting_down", lambda *a, **k: shutting_down)

    def execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        mod._announce_session(turn_id, "sid-1")
        if person_stop:
            mod.request_stop(turn_id, "cancel frame")
        return ok, "All done." if ok else "turn ended: cancelled", "sid-1"

    monkeypatch.setattr(mod, "execute_prompt", execute)
    mod._run_turn("r-1", {"id": TID, "agent_slug": "canopy-web", "prompt": "hi",
                          "origin_ref": {}})
    return [b["status"] for p, b in timeline if p == f"/turns/{TID}/finish"]


def test_a_turn_killed_by_the_box_powering_off_is_left_to_be_queued_again(run, monkeypatch):
    assert _finishes(run, monkeypatch, ok=False, stopping=True, shutting_down=True) == []
    mod, _ = run
    with mod._TURNS_LOCK:
        assert TID not in mod._IN_FLIGHT, "the worker still cleans up after itself"


def test_a_runner_restart_or_a_real_failure_is_still_reported(run, monkeypatch):
    assert _finishes(run, monkeypatch, ok=False, stopping=True, shutting_down=False) == ["failed"]


def test_a_failure_while_the_runner_is_not_stopping_is_reported(run, monkeypatch):
    assert _finishes(run, monkeypatch, ok=False, stopping=False, shutting_down=True) == ["failed"]


def test_a_turn_done_as_the_box_goes_down_is_still_done(run, monkeypatch):
    assert _finishes(run, monkeypatch, ok=True, stopping=True, shutting_down=True) == ["done"]


def test_a_persons_stop_still_finishes_cancelled_during_shutdown(run, monkeypatch):
    assert _finishes(run, monkeypatch, ok=False, stopping=True, shutting_down=True,
                     person_stop=True) == ["cancelled"]
