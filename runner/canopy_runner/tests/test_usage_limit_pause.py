"""The laptop runner parks itself when its Claude login hits a usage cap.

Every session on a box shares that box's one login, so a cap seen in ANY
session's transcript means the runner cannot work until the reset. The hook
listener spots Claude Code's own cap record and the runner pauses itself on
canopy-web with the reset, where the server schedules the unpause.
"""
import json

from canopy_runner import hooks
from canopy_runner.hook_listener import HookListener

CAP = "You've hit your session limit · resets 2:30am (America/Denver)"


def _cap_transcript(tmp_path, *, uuid="u-1", api_error=True):
    t = tmp_path / "t.jsonl"
    t.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n"
        + json.dumps({"type": "assistant", "uuid": uuid, "isApiErrorMessage": api_error,
                      "error": "rate_limit",
                      "message": {"model": "<synthetic>",
                                  "content": [{"type": "text", "text": CAP}]}}) + "\n"
        + json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    return t


def _listener(seen):
    return HookListener(port=0, nonce="n", resolve_session=lambda cwd: "",
                        forward=lambda: False, on_usage_limit=seen.append)


def test_a_cap_record_at_the_end_of_a_turn_pauses_the_box(tmp_path):
    seen = []
    t = _cap_transcript(tmp_path)
    _listener(seen).handle_payload({"hook_event_name": "Notification", "cwd": "/anywhere",
                                    "transcript_path": str(t)})
    assert seen == [CAP]


def test_it_fires_once_per_cap_not_once_per_hook(tmp_path):
    seen = []
    hl = _listener(seen)
    t = _cap_transcript(tmp_path)
    for event in ("Stop", "Notification", "Notification"):
        hl.handle_payload({"hook_event_name": event, "transcript_path": str(t)})
    assert len(seen) == 1


def test_an_agent_writing_about_limits_does_not_pause(tmp_path):
    seen = []
    t = _cap_transcript(tmp_path, api_error=False)
    _listener(seen).handle_payload({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert seen == []


def test_other_hook_kinds_never_read_the_transcript(tmp_path):
    seen = []
    t = _cap_transcript(tmp_path)
    _listener(seen).handle_payload({"hook_event_name": "PreToolUse", "transcript_path": str(t)})
    assert seen == []


class _Client:
    def __init__(self):
        self.calls = []

    def set_paused(self, runner_id, paused, note="", until=""):
        self.calls.append((runner_id, paused, note, until))


class _Cfg:
    runner_id = "rid-1"


def test_the_pause_carries_the_parsed_reset():
    client = _Client()
    hooks.pause_for_usage_limit(_Cfg(), client, CAP)
    (rid, paused, note, until), = client.calls
    assert (rid, paused) == ("rid-1", True)
    assert note.startswith("Claude usage cap — resumes") and CAP in note
    assert until  # ISO 8601, scheduled by the server


def test_an_unreadable_reset_falls_back_to_a_short_park():
    client = _Client()
    hooks.pause_for_usage_limit(_Cfg(), client, "You've hit your limit")
    note = client.calls[0][2]
    assert "unreadable" in note
