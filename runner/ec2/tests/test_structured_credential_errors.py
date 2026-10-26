"""A credential's failure is read from the CLI's structured fields, not its prose.

With an invalid or revoked setup-token, Claude Code 2.1.310 ends a `claude -p`
turn with "Failed to authenticate. API Error: 401 OAuth access token is
invalid." None of the runner's text markers match that sentence, so the box
neither failed over to its next login nor sent the auth-required alert, and the
probe called the token `unknown`. The stream says it plainly, though: the
assistant message carries `error: "authentication_failed"` and the result
`api_error_status: 401`. The event shapes below are the ones the CLI emitted in
that live reproduction (docs/superpowers/specs/2026-10-26-claude-code-audit.md,
finding 1).
"""
from __future__ import annotations

import json
import subprocess

import pytest

TEXT_401 = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
TID = "turn-401a2b3c"


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _stream_401(session_id: str = "s-dead") -> list[str]:
    return [
        _line({"type": "system", "subtype": "init", "session_id": session_id}),
        _line({"type": "system", "subtype": "api_retry", "session_id": session_id,
               "error": "authentication_failed", "error_status": 401}),
        _line({"type": "assistant", "session_id": session_id,
               "error": "authentication_failed", "is_api_error_message": True,
               "api_error_status": 401,
               "message": {"content": [{"type": "text", "text": TEXT_401}]}}),
        _line({"type": "result", "session_id": session_id, "is_error": True,
               "terminal_reason": "api_error", "api_error_status": 401,
               "result": TEXT_401}),
    ]


def _stream_ok(text: str = "did the work", session_id: str = "s-ok",
               limit_status: str = "allowed") -> list[str]:
    return [
        _line({"type": "system", "subtype": "init", "session_id": session_id}),
        _line({"type": "rate_limit_event", "session_id": session_id,
               "rate_limit_info": {"status": limit_status, "rateLimitType": "five_hour",
                                   "resetsAt": 1_900_000_000}}),
        _line({"type": "assistant", "session_id": session_id,
               "message": {"content": [{"type": "text", "text": text}]}}),
        _line({"type": "result", "session_id": session_id, "is_error": False,
               "result": text}),
    ]


def _stream_rejected(resets_at: int, session_id: str = "s-cap") -> list[str]:
    # A cap worded in a way no text matcher knows: only the event says so.
    return [
        _line({"type": "system", "subtype": "init", "session_id": session_id}),
        _line({"type": "rate_limit_event", "session_id": session_id,
               "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day",
                                   "resetsAt": resets_at}}),
        _line({"type": "assistant", "session_id": session_id, "error": "rate_limit",
               "message": {"content": [{"type": "text", "text": "Out of credit for now."}]}}),
        _line({"type": "result", "session_id": session_id, "is_error": True,
               "result": "Out of credit for now."}),
    ]


class _FakeProc:
    def __init__(self, lines, returncode: int):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-1")
    cloud_runner._CLAUDE_CREDS.clear()
    cloud_runner._CLAUDE_CREDS.extend([
        ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
        ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "tok-2"),
    ])
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cloud_runner, "box_login_ready", lambda *a, **k: False)
    monkeypatch.setattr(cloud_runner, "_reload_claude_credentials", lambda: False)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {}))
    alerts, outcomes = [], []
    monkeypatch.setattr(cloud_runner, "_notify_auth_required",
                        lambda label, turn_id: alerts.append(label))
    monkeypatch.setattr(cloud_runner, "_record_outcome",
                        lambda label, status, **kw: outcomes.append((label, status, kw)))
    cloud_runner.alerts, cloud_runner.outcomes = alerts, outcomes
    cloud_runner.test_cwd = tmp_path
    cloud_runner._apply_claude_credential(0)
    return cloud_runner


def _streams(cr, monkeypatch, by_label: dict):
    """Each spawn of `claude -p` streams what the credential in use would."""
    seen = []

    def popen(*a, **k):
        label = cr._claude_cred_label()
        seen.append(label)
        lines, code = by_label[label]
        return _FakeProc(lines, code)

    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    return seen


def _run(cr):
    events = []
    ok, text, sid = cr.execute_prompt("do it", TID, events.extend, cwd=cr.test_cwd)
    return ok, text, sid, events


def test_the_text_matchers_alone_miss_the_401(cr):
    """The bug, pinned: the sentence is neither a cap nor a sign-in request to
    the matchers, which is why the structured fields have to decide."""
    assert not cr._is_usage_cap(TEXT_401)
    assert not cr._is_auth_required(TEXT_401)


def test_a_401_fails_over_to_the_next_login_and_alerts(cr, monkeypatch):
    seen = _streams(cr, monkeypatch, {
        "subscription-1": (_stream_401(), 1),
        "subscription-2": (_stream_ok(), 0),
    })
    ok, text, sid, events = _run(cr)

    assert ok is True and text == "did the work" and sid == "s-ok"
    assert seen == ["subscription-1", "subscription-2"]
    assert cr.alerts == ["subscription-1"], "a dead token must raise the auth-required alert"
    assert ("subscription-1", "dead") in [(o[0], o[1]) for o in cr.outcomes]
    dead = next(o for o in cr.outcomes if o[1] == "dead")
    assert "401" in dead[2]["detail"]
    # The dead login's own sentence is held back: the next login answered.
    said = [e["payload"]["text"] for e in events if e["kind"] == "assistant"]
    assert said == ["did the work"]
    assert all("credential_error" not in e for e in events)


def test_every_login_401_says_not_signed_in(cr, monkeypatch):
    _streams(cr, monkeypatch, {
        "subscription-1": (_stream_401(), 1),
        "subscription-2": (_stream_401(), 1),
    })
    ok, text, _sid, events = _run(cr)

    assert ok is False
    assert "not signed in to Claude" in text
    assert "will NOT clear on its own" in text
    assert cr.alerts == ["subscription-1", "subscription-2"]
    # The turn ended on that attempt, so what the CLI said is shown — once, untagged.
    notices = [e for e in events if e["kind"] == "assistant"]
    assert [e["payload"]["text"] for e in notices] == [TEXT_401]
    assert all("credential_error" not in e for e in notices)


def test_a_rejected_rate_limit_is_a_cap_with_its_exact_reset(cr, monkeypatch):
    resets_at = 4_102_444_800  # 2100-01-01, safely in the future
    _streams(cr, monkeypatch, {
        "subscription-1": (_stream_rejected(resets_at), 1),
        "subscription-2": (_stream_ok(), 0),
    })
    ok, text, _sid, _events = _run(cr)

    assert ok is True and text == "did the work"
    assert cr.alerts == [], "a cap is not a dead login"
    capped = [o for o in cr.outcomes if o[1] == "capped"]
    assert capped and capped[0][0] == "subscription-1"
    assert capped[0][2]["reset"] == float(resets_at)


def test_an_allowed_rate_limit_on_a_good_turn_changes_nothing(cr, monkeypatch):
    """The event rides on every successful turn; only `rejected` counts."""
    seen = _streams(cr, monkeypatch, {"subscription-1": (_stream_ok(), 0)})
    ok, _text, _sid, _events = _run(cr)
    assert ok is True and seen == ["subscription-1"]
    assert TID not in cr._CREDENTIAL_FAULT and TID not in cr._CAP_RESET_HINT


def test_an_ordinary_failure_with_no_structured_fault_does_not_fail_over(cr, monkeypatch):
    lines = [
        _line({"type": "system", "subtype": "init", "session_id": "s1"}),
        _line({"type": "result", "session_id": "s1", "is_error": True,
               "result": "AssertionError: the agent's own bug"}),
    ]
    seen = _streams(cr, monkeypatch, {"subscription-1": (lines, 1)})
    ok, _text, _sid, _events = _run(cr)
    assert ok is False and seen == ["subscription-1"]
    assert cr.alerts == []


@pytest.mark.parametrize("evt, expected", [
    ({"type": "assistant", "error": "authentication_failed"}, ("dead", None)),
    ({"type": "assistant", "error": "oauth_org_not_allowed"}, ("dead", None)),
    ({"type": "assistant", "error": "billing_error"}, ("no_credit", None)),
    ({"type": "assistant", "error": "overloaded"}, (None, None)),
    ({"type": "assistant"}, (None, None)),
    ({"type": "system", "subtype": "api_retry", "error": "authentication_failed"}, ("dead", None)),
    # The CLI retries a rate limit itself; a retry is not a cap.
    ({"type": "system", "subtype": "api_retry", "error": "rate_limit"}, (None, None)),
    ({"type": "result", "is_error": True, "api_error_status": 401}, ("dead", None)),
    ({"type": "result", "is_error": False, "api_error_status": 401}, (None, None)),
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}, (None, None)),
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 5}},
     ("capped", 5.0)),
])
def test_credential_fault_reads_each_event(cr, evt, expected):
    assert cr._credential_fault(evt) == expected


def test_the_probe_calls_a_401_token_dead(cr, monkeypatch):
    """The probe ran the same sentence through the same matchers and answered
    `unknown`, which counts as possibly working."""
    monkeypatch.setattr(cr, "PROBE_ENABLED", True)
    doc = {"type": "result", "is_error": True, "api_error_status": 401,
           "terminal_reason": "api_error", "result": TEXT_401}
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout=json.dumps(doc), stderr=""))
    out = cr._probe_entry(("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"))
    assert out["status"] == "dead"
    assert "401" in out["detail"]
