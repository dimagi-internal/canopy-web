"""A laptop whose Claude login is gone stops claiming and says why.

2026-10-22: acedimagi-mbp-cdp had no Claude sign-in. Each message it took
"answered" with `Not logged in · Please run /login`, and since the turn finishes
at delivery the box stayed ready and kept claiming. Now the hook listener reads
the CLI's own API-error record, the box marks itself signed out, raises the
`claude_auth_required` alert once, and clears the mark the moment Claude answers
anything on the box again.
"""
import json
from types import SimpleNamespace

from canopy_transcript import SIGNED_OUT_NOTE

from canopy_runner import hooks, readiness
from canopy_runner.hook_listener import HookListener

SIGNED_OUT = {
    "type": "assistant", "uuid": "u-1", "isApiErrorMessage": True,
    "message": {"model": "<synthetic>",
                "content": [{"type": "text", "text": "Not logged in · Please run /login"}]},
}
REPLY = {
    "type": "assistant", "uuid": "u-2",
    "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Here."}]},
}


def _cfg(tmp_path):
    return SimpleNamespace(state_path=str(tmp_path / "runner-state.json"), cdp_port=9222,
                           runner_id="r-1")


def _transcript(tmp_path, *records):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(p)


def _stop(path):
    return {"hook_event_name": "Stop", "cwd": "/elsewhere", "transcript_path": path}


def test_the_listener_reports_signed_out_once_and_answered_after(tmp_path):
    out, back = [], []
    lis = HookListener(port=0, nonce="n", resolve_session=lambda c: "", forward=lambda: False,
                       on_signed_out=out.append, on_answered=lambda: back.append(1))
    path = _transcript(tmp_path, SIGNED_OUT)
    lis.handle_payload(_stop(path))
    lis.handle_payload({**_stop(path), "hook_event_name": "Notification"})
    assert out == ["Not logged in · Please run /login"]     # one record, one report
    assert back == []
    lis.handle_payload(_stop(_transcript(tmp_path, SIGNED_OUT, REPLY)))
    assert back == [1]


def test_signed_out_marks_not_ready_and_alerts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    alerts = []
    client = SimpleNamespace(post_alert=alerts.append)
    hooks.report_signed_out(cfg, client, "Not logged in · Please run /login")
    hooks.report_signed_out(cfg, client, "Not logged in · Please run /login")
    ready, note = readiness.compute(cfg)
    assert ready is False and note.startswith(SIGNED_OUT_NOTE)
    assert readiness.is_signed_out(cfg)
    assert [a["kind"] for a in alerts] == ["claude_auth_required"]


def test_an_answer_clears_signed_out_but_no_other_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    readiness.mark_failed(cfg, "runner execute crashed: boom")
    hooks.note_signed_in(cfg)
    assert readiness.compute(cfg) == (False, "runner execute crashed: boom")
    readiness.mark_signed_out(cfg)
    hooks.note_signed_in(cfg)
    assert readiness.compute(cfg) == (True, "")
