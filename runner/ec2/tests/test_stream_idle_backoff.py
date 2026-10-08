"""#647: the cloud runner's stream clock backs off once transcripts go quiet.

At 3s an idle box asked /streams 1,200 times an hour for nothing. A viewer
attaching still rings the `stream` doorbell (an immediate sync), and the first
growth snaps the clock back to its live cadence.
"""
from __future__ import annotations

import pathlib


def test_stream_clock_backs_off_once_transcripts_go_quiet(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "STREAM_POLL_SECONDS", 3.0)
    monkeypatch.setattr(cloud_runner, "STREAM_IDLE_AFTER_SECONDS", 60.0)
    monkeypatch.setattr(cloud_runner, "STREAM_IDLE_POLL_SECONDS", 15.0)
    monkeypatch.setattr(cloud_runner, "_LAST_STREAM_ACTIVITY", 1000.0)
    assert cloud_runner._stream_interval(1030.0) == 3.0    # recently live
    assert cloud_runner._stream_interval(1061.0) == 15.0   # quiet for a minute
    # An idle clock set below the live one never makes the live view slower.
    monkeypatch.setattr(cloud_runner, "STREAM_IDLE_POLL_SECONDS", 1.0)
    assert cloud_runner._stream_interval(5000.0) == 3.0


def test_a_growing_transcript_marks_stream_activity(cloud_runner, monkeypatch, tmp_path):
    class _Reader:
        def read_new(self):
            return [{"type": "user"}]

    class _Core:
        @staticmethod
        def conversational_messages(records, *_a, **_k):
            return []

    path = pathlib.Path(tmp_path) / "abc.jsonl"
    monkeypatch.setattr(cloud_runner, "_transcript_core", lambda: _Core)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (
        200, {"streams": [{"session_id": "s1", "session_key": "k"}]}))
    monkeypatch.setattr(cloud_runner, "_session_transcript_path", lambda *a: path)
    monkeypatch.setattr(cloud_runner, "_STREAM_READERS", {
        "s1": {"reader": _Reader(), "count": 0, "transcript_id": "abc", "path": str(path)}})
    monkeypatch.setattr(cloud_runner, "_LAST_STREAM_ACTIVITY", 0.0)
    cloud_runner._sync_session_streams("r1")
    assert cloud_runner._LAST_STREAM_ACTIVITY > 0.0


def test_a_quiet_sync_leaves_the_clock_idle(cloud_runner, monkeypatch, tmp_path):
    class _Reader:
        def read_new(self):
            return []

    path = pathlib.Path(tmp_path) / "abc.jsonl"
    monkeypatch.setattr(cloud_runner, "_transcript_core", lambda: object())
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (
        200, {"streams": [{"session_id": "s1", "session_key": "k"}]}))
    monkeypatch.setattr(cloud_runner, "_session_transcript_path", lambda *a: path)
    monkeypatch.setattr(cloud_runner, "_STREAM_READERS", {
        "s1": {"reader": _Reader(), "count": 0, "transcript_id": "abc", "path": str(path)}})
    monkeypatch.setattr(cloud_runner, "_LAST_STREAM_ACTIVITY", 0.0)
    cloud_runner._sync_session_streams("r1")
    assert cloud_runner._LAST_STREAM_ACTIVITY == 0.0
