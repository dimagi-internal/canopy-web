"""Two credential faults the runner used to misread.

An API key whose account has no credit (`billing_error`, "Your credit balance is
too low") was counted as a usage cap: the box parked and waited for a reset that
never comes, and nobody was told. It is its own state now — the box moves on
and pauses like a cap, and the runner's owner is alerted.

An ACP turn the API refused could end with a clean stop and the error as plain
text, so the runner could not tell a dead token from a success. Claude Code
writes its own record of the refusal (`isApiErrorMessage` with its `error`
category) into the session's transcript, which the ACP adapter shares; the
runner reads it (`_transcript_fault`).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from test_structured_credential_errors import _FakeProc, _line, _run, _stream_ok

TEXT_BROKE = "Your credit balance is too low to access the Anthropic API."


def _stream_broke(session_id: str = "s-broke") -> list[str]:
    return [
        _line({"type": "system", "subtype": "init", "session_id": session_id}),
        _line({"type": "assistant", "session_id": session_id, "error": "billing_error",
               "message": {"content": [{"type": "text", "text": TEXT_BROKE}]}}),
        _line({"type": "result", "session_id": session_id, "is_error": True,
               "result": TEXT_BROKE}),
    ]


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-1")
    cloud_runner._CLAUDE_CREDS.clear()
    cloud_runner._CLAUDE_CREDS.extend([
        ("api-key:aa", "ANTHROPIC_API_KEY", "key-1"),
        ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
    ])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cloud_runner, "box_login_ready", lambda *a, **k: False)
    monkeypatch.setattr(cloud_runner, "_reload_claude_credentials", lambda: False)
    calls = []
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (calls.append((a, k)), (200, {}))[1])
    monkeypatch.setattr(cloud_runner, "_notify_api_key_fallback", lambda *a, **k: None)
    outcomes = []
    monkeypatch.setattr(cloud_runner, "_record_outcome",
                        lambda label, status, **kw: outcomes.append((label, status)))
    cloud_runner.calls, cloud_runner.outcomes = calls, outcomes
    cloud_runner.test_cwd = tmp_path
    cloud_runner._RUNNER_ID = "runner-1"
    cloud_runner._apply_claude_credential(0)
    return cloud_runner


def _streams(cr, monkeypatch, by_label):
    def popen(*a, **k):
        lines, code = by_label[cr._claude_cred_label()]
        return _FakeProc(lines, code)
    monkeypatch.setattr(cr.subprocess, "Popen", popen)


def _events_posted(cr, kind):
    return [a[2]["items"][0] for a, k in cr.calls
            if len(a) > 2 and k.get("prefix") == "/api/events" and a[2]["items"][0]["kind"] == kind]


def test_no_credit_is_its_own_fault_not_a_cap(cr):
    assert cr._credential_fault({"type": "assistant", "error": "billing_error"}) == ("no_credit", None)
    assert cr._credential_fault({"type": "assistant", "error": "rate_limit"}) == ("capped", None)
    assert cr._is_out_of_credit(TEXT_BROKE) and not cr._is_usage_cap(TEXT_BROKE)


def test_an_empty_account_fails_over_and_alerts_the_owner(cr, monkeypatch):
    _streams(cr, monkeypatch, {"api-key:aa": (_stream_broke(), 1),
                               "subscription-1": (_stream_ok(), 0)})
    ok, text, _sid, _events = _run(cr)
    assert ok and text == "did the work"
    assert ("api-key:aa", "no_credit") in cr.outcomes
    (alert,) = _events_posted(cr, "claude_credit_exhausted")
    assert "out of credit" in alert["summary"] and alert["level"] == "error"


def test_when_every_credential_is_broke_the_box_pauses_and_says_why(cr, monkeypatch):
    _streams(cr, monkeypatch, {"api-key:aa": (_stream_broke(), 1),
                               "subscription-1": (_stream_broke(), 1)})
    ok, text, _sid, _events = _run(cr)
    assert not ok
    assert "out of credit" in text and "will NOT clear on its own" in text
    pauses = [a for a, k in cr.calls if len(a) > 1 and a[1] == "/runners/runner-1/pause"]
    assert pauses and pauses[0][2]["note"].startswith(cr.SELF_PAUSE_PREFIX + "out of credit")
    assert not _events_posted(cr, "claude_usage_exhausted")


def test_the_probe_reports_no_credit_as_needing_a_person(cr, monkeypatch):
    checks = {}
    monkeypatch.setattr(cr, "_set_check", lambda name, status, detail: checks.update({name: (status, detail)}))
    cr._record_probe({"label": "api-key:aa", "status": "no_credit", "detail": TEXT_BROKE, "at": 0})
    status, detail = checks["claude.probe.api-key:aa"]
    assert status == "fail" and detail.startswith("out of credit")


# ---- ACP: the transcript says what the adapter did not ----
def _write(tmp_path, monkeypatch, cr, records, session_id="sess-1"):
    target = tmp_path / f"{session_id}.jsonl"
    target.write_text("".join(json.dumps(r) + "\n" for r in records))
    ct = cr._transcript_core()
    monkeypatch.setattr(ct, "resolve_cli_transcript", lambda cwd, sid, **kw: target if sid == session_id else None)
    return session_id


def _at(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _api_error(error, text, when):
    return {"type": "assistant", "isApiErrorMessage": True, "error": error, "timestamp": when,
            "message": {"content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize("error,text,fault", [
    ("authentication_failed", "Failed to authenticate. API Error: 401", "dead"),
    ("rate_limit", "You've hit your weekly limit", "capped"),
    ("billing_error", TEXT_BROKE, "no_credit"),
    (None, "Not logged in · Please run /login", "dead"),
])
def test_a_turn_ending_on_an_api_error_names_its_fault(cr, tmp_path, monkeypatch, error, text, fault):
    since = dt.datetime.now(dt.timezone.utc).timestamp() - 30
    sid = _write(tmp_path, monkeypatch, cr, [
        {"type": "user", "timestamp": _at(20), "message": {"content": "hi"}},
        _api_error(error, text, _at(10)),
        {"type": "system", "timestamp": _at(9)},
    ])
    assert cr._transcript_fault(tmp_path, sid, since) == fault


def test_an_earlier_turns_error_or_a_good_ending_is_no_fault(cr, tmp_path, monkeypatch):
    since = dt.datetime.now(dt.timezone.utc).timestamp() - 5
    sid = _write(tmp_path, monkeypatch, cr, [_api_error("authentication_failed", "401", _at(60))])
    assert cr._transcript_fault(tmp_path, sid, since) is None
    sid = _write(tmp_path, monkeypatch, cr, [
        _api_error("authentication_failed", "401", _at(4)),
        {"type": "assistant", "timestamp": _at(2), "message": {"content": [{"type": "text", "text": "ok"}]}},
    ], session_id="sess-2")
    assert cr._transcript_fault(tmp_path, sid, since - 10) is None
    assert cr._transcript_fault(tmp_path, "", since) is None
