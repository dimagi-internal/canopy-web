"""A turn's reply that runs past the note cap says so and links the rest.

canopy-web#1955: the finish stored `text[:2000]` with no marker. 12 of 17 replies a
member read through `canopy agent turns` were exactly 2,000 characters, and their
Claude dispatched follow-ups — new sessions with no memory — to get the rest.
"""
from __future__ import annotations

import pytest

TID = "3f2b8c1e-0000-4000-8000-000000000011"
LONG = "The review found three issues. " * 200   # ~6,200 characters


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.example.org")
    monkeypatch.setattr(cloud_runner, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(cloud_runner, "_start_lease_renewal",
                        lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(cloud_runner, "_child_safe_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(cloud_runner, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(cloud_runner, "_ship_transcript_rows", lambda *a: None)
    api = []
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, b=None, **k: api.append((m, p, b)) or (200, {}))
    reply = {"text": LONG}
    monkeypatch.setattr(cloud_runner, "execute_prompt",
                        lambda prompt, turn_id, emit, cwd=None, agent_slug=None,
                        resume_session_id=None: (True, reply["text"], "cli-session-1"))
    return cloud_runner, api, reply


def _finish(api):
    bodies = [b for m, p, b in api if p.endswith("/finish")]
    assert len(bodies) == 1
    return bodies[0]


def _turn():
    return {"id": TID, "agent_slug": "ace", "workspace_slug": "connect",
            "prompt": "review the labs dashboard", "origin_ref": {"thread_key": "dispatch-x"}}


def test_a_long_reply_is_cut_with_a_marker_linking_the_turn(cr):
    mod, api, _ = cr
    mod._run_turn("r-1", _turn())
    note = _finish(api)["result_note"]
    assert len(note) <= 2000
    assert note.endswith(
        f"… [truncated — full reply: https://canopy.example.org/w/connect/agents/ace/turns#{TID}]")
    assert note.startswith("The review found three issues.")


def test_a_short_reply_is_sent_whole_with_no_marker(cr):
    mod, api, reply = cr
    reply["text"] = "All clear."
    mod._run_turn("r-1", _turn())
    assert _finish(api)["result_note"] == "All clear."


def test_without_canopy_transcript_the_finish_still_lands(cr, monkeypatch):
    mod, api, _ = cr
    monkeypatch.setattr(mod, "_transcript_core", lambda: None)
    mod._run_turn("r-1", _turn())
    assert _finish(api)["result_note"] == LONG[:2000]
