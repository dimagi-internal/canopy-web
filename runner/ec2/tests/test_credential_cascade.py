"""The Claude credential cascade: subscription-1 -> subscription-2 -> API key.

A subscription's weekly cap takes out EVERY agent on the box at once, and an
unattended agent has nobody to tell (2026-08-01: the whole fleet sat dead on one
exhausted login). These tests pin the behaviour that fixes it, and — just as
important — the behaviour that must NOT happen: an ordinary turn failure being
re-run against every credential in turn, spending real money on a plain bug.
"""
from __future__ import annotations

import calendar

import pytest

from _bundles import bundle

CAP_TEXT = "You've hit your weekly limit · resets Aug 3, 11pm (UTC)"


@pytest.fixture()
def cr(cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-1")
    cloud_runner._CLAUDE_CREDS.clear()
    cloud_runner._CLAUDE_CREDS.extend([
        ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
        ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "tok-2"),
        ("api-key", "ANTHROPIC_API_KEY", "sk-ant-xxx"),
    ])
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No box login, whatever machine runs this: the cascade puts a signed-in
    # login on disk (CLAUDE_CONFIG_DIR, ~/.claude) ahead of the bundle, so on a
    # box that has one — any cloud runner — a re-read picked the box's REAL
    # login instead of the token this file stages, and the suite failed there
    # while passing in CI.
    monkeypatch.setattr(cloud_runner, "box_login_ready", lambda *a, **k: False)
    return cloud_runner


def test_detects_the_cap_message_the_fleet_actually_hit(cr):
    assert cr._is_usage_cap(CAP_TEXT)
    assert cr._is_usage_cap("Error: usage limit reached")
    assert cr._is_usage_cap("your credit balance is too low")


def test_an_ordinary_failure_is_not_a_cap(cr):
    """The expensive false positive: retrying a real bug down the whole cascade."""
    assert not cr._is_usage_cap("TypeError: 'NoneType' object is not subscriptable")
    assert not cr._is_usage_cap("")


def test_selecting_a_credential_clears_the_other_auth_var(cr, monkeypatch):
    """claude prefers CLAUDE_CODE_OAUTH_TOKEN when both are set — so stepping to
    the API key MUST clear the OAuth var or the fallback silently never happens."""
    import os

    cr._apply_claude_credential(0)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"
    assert "ANTHROPIC_API_KEY" not in os.environ

    cr._apply_claude_credential(2)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xxx"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_advance_walks_the_cascade_then_reports_exhaustion(cr, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    assert cr._advance_claude_credential() is True
    assert cr._claude_cred_label() == "subscription-2"
    assert cr._advance_claude_credential() is True
    assert cr._claude_cred_label() == "api-key"
    assert cr._advance_claude_credential() is False  # nothing left


def test_falling_back_to_the_api_key_notifies(cr, monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_api", lambda m, p, b=None, **kw: (calls.append((m, p, b, kw)) or (200, {})))
    cr._apply_claude_credential(1)          # on subscription-2
    cr._advance_claude_credential()          # -> api-key
    assert calls, "stepping onto metered billing must notify"
    method, path, body, kw = calls[0]
    assert (method, path) == ("POST", "/")
    assert kw.get("prefix") == "/api/events"       # events router, not harness
    assert "items" in body                          # EventBatchIn shape
    assert body["items"][0]["kind"] == "claude_api_key_fallback"
    assert body["items"][0]["level"] == "warn"


def test_no_notify_when_stepping_between_subscriptions(cr, monkeypatch):
    """Only metered billing is worth waking someone for."""
    calls = []
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (calls.append(a) or (200, {})))
    cr._apply_claude_credential(0)
    cr._advance_claude_credential()          # -> subscription-2
    assert calls == []


def test_a_notify_failure_never_breaks_the_turn(cr, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("canopy-web unreachable")

    monkeypatch.setattr(cr, "_api", boom)
    cr._apply_claude_credential(1)
    assert cr._advance_claude_credential() is True   # still advanced
    assert cr._claude_cred_label() == "api-key"


def test_execute_prompt_retries_the_turn_on_the_next_credential(cr, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    seen = []

    def fake_once(prompt, turn_id, emit, **kw):
        seen.append(cr._claude_cred_label())
        if cr._claude_cred_label() == "subscription-1":
            return False, CAP_TEXT, ""
        return True, "did the work", "sess-1"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    ok, text, sid = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert ok is True
    assert text == "did the work"
    assert seen == ["subscription-1", "subscription-2"]


def test_execute_prompt_does_not_retry_an_ordinary_failure(cr, monkeypatch):
    cr._apply_claude_credential(0)
    calls = []

    def fake_once(prompt, turn_id, emit, **kw):
        calls.append(1)
        return False, "AssertionError: the agent's own bug", ""

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert ok is False
    assert len(calls) == 1, "a plain failure must not burn the whole cascade"
    assert cr._claude_cred_label() == "subscription-1"


def test_exhausting_every_credential_says_so_in_the_turn_text(cr, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    monkeypatch.setattr(cr, "_execute_once",
                        lambda *a, **k: (False, CAP_TEXT, ""))
    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert ok is False
    # The bare cap message names a reset time but never says the box is out of
    # credentials — which is the part a human has to act on.
    assert "every Claude credential on this box is exhausted" in text
    assert "subscription-1" in text and "api-key" in text


def test_retry_resumes_the_callers_session_not_a_fresh_one(cr, monkeypatch):
    """A cap must not cost a session its conversation (Hal f6e30263, 2026-10-19):
    every retry resumes the session the caller named, and a turn whose attempts
    all fail still reports that session, not the fresh one a failed attempt opened."""
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_notify_out_of_usage", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_park_until_cap_resets", lambda *a, **k: "later")
    monkeypatch.setattr(cr, "_reload_claude_credentials", lambda: False)
    cr._apply_claude_credential(0)
    resumes = []

    def fake_once(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        resumes.append(resume_session_id)
        return False, CAP_TEXT, resume_session_id or "fresh"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    ok, _, session_id = cr.execute_prompt(
        "p", "turn-abcdef12", lambda e: None, resume_session_id="sess-old")
    assert ok is False
    assert resumes == ["sess-old", "sess-old", "sess-old"]
    assert session_id == "sess-old"


def test_retry_without_a_session_stays_fresh(cr, monkeypatch):
    """A first turn's failed attempt may leave a partial session; never reuse it."""
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    resumes = []

    def fake_once(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        resumes.append(resume_session_id)
        if len(resumes) == 1:
            return False, CAP_TEXT, "partial"
        return True, "ok", "s"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert resumes == [None, None]


# --- mid-run reload: rescuing a box without restarting it --------------------
def test_exhausted_cascade_rereads_credentials_before_giving_up(cr, monkeypatch):
    """Credentials are staged into the process at start-up, so an operator who
    rescues a stuck box by signing in a new token would otherwise have to
    restart the service for the fix to land."""
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._CLAUDE_CREDS.clear()
    cr._CLAUDE_CREDS.append(("token:1", "CLAUDE_CODE_OAUTH_TOKEN", "old"))
    cr._apply_claude_credential(0)
    cr._CLAUDE_CRED_RUNNER_ID = "r-1"

    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, bundle("rescued")))
    calls = []

    def fake_once(prompt, turn_id, emit, **kw):
        calls.append(cr._CLAUDE_CREDS[cr._CLAUDE_CRED_I][2])
        return (False, CAP_TEXT, "") if len(calls) == 1 else (True, "ok", "s")

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    ok, text, _ = cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert ok is True
    assert calls == ["old", "rescued"]


def test_reload_that_finds_the_same_values_does_not_loop(cr, monkeypatch):
    """The guard against spinning forever on an unchanged bundle."""
    cr._CLAUDE_CREDS.clear()
    cr._CLAUDE_CREDS.append(("token:1", "CLAUDE_CODE_OAUTH_TOKEN", "same"))
    cr._apply_claude_credential(0)
    cr._CLAUDE_CRED_RUNNER_ID = "r-1"
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, bundle("same")))
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, CAP_TEXT, ""))
    ok, text, _ = cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert ok is False
    assert "exhausted" in text


def test_the_primary_working_promotes_nothing(cr, monkeypatch):
    monkeypatch.setattr(cr, "_CLAUDE_CRED_RUNNER_ID", "r-1")
    calls = []
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (calls.append(a) or (200, {})))
    cr._apply_claude_credential(0)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (True, "ok", "s"))
    cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert calls == []


def test_the_api_key_is_never_promoted(cr, monkeypatch):
    monkeypatch.setattr(cr, "_CLAUDE_CRED_RUNNER_ID", "r-1")
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (calls.append(a) or (200, {})))
    cr._apply_claude_credential(0)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (
        (False, CAP_TEXT, "") if cr._CLAUDE_CRED_I < 2 else (True, "ok", "s")))
    cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert not any("swap" in str(c) for c in calls)


# --- parking the box until the cap resets -------------------------------------

def _capture_pause(cr, monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid-1")
    monkeypatch.setattr(cr, "_reload_claude_credentials", lambda: False)
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_api", lambda m, p, b=None, **kw: (calls.append((m, p, b)) or (200, {})))
    return calls


def test_an_exhausted_cascade_pauses_the_runner_until_the_earliest_reset(cr, monkeypatch):
    import time as _time
    calls = _capture_pause(cr, monkeypatch)
    now = _time.time()
    resets = iter([now + 7200, now + 3600, now + 5400])   # sub-2 frees up first
    monkeypatch.setattr(cr, "_cap_reset_epoch", lambda text: next(resets))
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, CAP_TEXT, ""))
    cr._apply_claude_credential(0)

    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)

    assert ok is False
    pauses = [c for c in calls if c[1] == "/runners/rid-1/pause"]
    assert len(pauses) == 1
    body = pauses[0][2]
    assert body["until"] == _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + 3600))
    assert body["note"].startswith(cr.SELF_PAUSE_PREFIX + "Claude usage cap — resumes")
    assert "paused itself until" in text
    # Back to the primary for when the pause lifts.
    assert cr._claude_cred_label() == "subscription-1"


def test_an_unreadable_reset_still_parks_for_the_fallback_window(cr, monkeypatch):
    import time as _time
    calls = _capture_pause(cr, monkeypatch)
    monkeypatch.setattr(cr, "_cap_reset_epoch", lambda text: None)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, "You've hit your limit", ""))
    cr._apply_claude_credential(0)
    before = _time.time()
    cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    body = [c for c in calls if c[1] == "/runners/rid-1/pause"][0][2]
    assert "unreadable" in body["note"]
    # timegm, not mktime() - timezone: `timezone` is the STANDARD offset, so
    # that form is an hour off during DST anywhere but UTC (red on a laptop,
    # green in CI).
    parked = calendar.timegm(_time.strptime(body["until"], "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(parked - (before + cr.CAP_PAUSE_FALLBACK_SECONDS)) < 5


def test_a_cap_the_next_credential_survives_does_not_pause(cr, monkeypatch):
    calls = _capture_pause(cr, monkeypatch)
    outcomes = iter([(False, CAP_TEXT, ""), (True, "done", "s")])
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: next(outcomes))
    cr._apply_claude_credential(0)
    ok, _, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert ok is True
    assert not [c for c in calls if c[1].endswith("/pause")]


def test_the_real_cap_text_parses_to_a_reset(cr):
    assert cr._cap_reset_epoch("You've hit your session limit · resets 2:30am (America/Denver)")


# --- out of usage: an alert, and ACP's bare refusal after a cap --------------
def _two_creds_after_a_cap(cr, monkeypatch, second_text):
    import time as _time
    calls = _capture_pause(cr, monkeypatch)
    alerts = {"auth": [], "usage": []}
    monkeypatch.setattr(cr, "_notify_auth_required",
                        lambda label, tid: alerts["auth"].append(label))
    monkeypatch.setattr(cr, "_notify_out_of_usage",
                        lambda capped, dead, until, tid: alerts["usage"].append((capped, dead)))
    monkeypatch.setattr(cr, "_cap_reset_epoch", lambda text: _time.time() + 5400)
    cr._CLAUDE_CREDS[:] = [("box-login:a44d94c3", "", ""),
                           ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    outcomes = iter([
        (False, "You've hit your session limit · resets 10:50pm (UTC)", "s1"),
        (False, second_text, ""),
    ])
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: next(outcomes))
    cr._apply_claude_credential(0)
    ok, text, _ = cr.execute_prompt("do it", "turn-7d230f39", lambda e: None)
    return ok, text, calls, alerts


def test_a_bare_acp_refusal_after_a_cap_is_out_of_usage_not_signed_out(cr, monkeypatch):
    """Investigation 9 (turn 7d230f39, ACE on cloud-ec2-2, 2026-10-18): every
    credential was out of usage, but the setup-token's cap came back as ACP's bare
    "Authentication required" and the note called the box "not signed in"."""
    ok, text, calls, alerts = _two_creds_after_a_cap(
        cr, monkeypatch, "runner error (acp): Authentication required")

    assert ok is False
    assert "not signed in" not in text
    assert "out of Claude usage" in text
    assert "You've hit your session limit" in text
    assert "paused itself until" in text
    assert [c for c in calls if c[1] == "/runners/rid-1/pause"]
    assert alerts == {"auth": [],
                      "usage": [(["box-login:a44d94c3", "subscription-1"], [])]}


def test_a_signed_out_token_after_a_cap_still_says_so(cr, monkeypatch):
    """The CLI's own sentence says which it is, so it is not folded into the cap."""
    ok, text, calls, alerts = _two_creds_after_a_cap(
        cr, monkeypatch, "runner error (acp): Authentication required\nOAuth session expired")

    assert "out of Claude usage" in text
    assert "subscription-1 is not signed in" in text
    assert alerts == {"auth": ["subscription-1"],
                      "usage": [(["box-login:a44d94c3"], ["subscription-1"])]}


def test_a_bare_acp_refusal_with_no_cap_is_not_signed_in(cr, monkeypatch):
    calls = _capture_pause(cr, monkeypatch)
    alerts = {"auth": [], "usage": []}
    monkeypatch.setattr(cr, "_notify_auth_required",
                        lambda label, tid: alerts["auth"].append(label))
    monkeypatch.setattr(cr, "_notify_out_of_usage",
                        lambda *a: alerts["usage"].append(a))
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (
        False, "runner error (acp): Authentication required", ""))
    cr._apply_claude_credential(0)

    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)

    assert "this box is not signed in" in text
    assert alerts == {"auth": ["subscription-1"], "usage": []}
    assert not [c for c in calls if c[1] == "/runners/rid-1/pause"]


def test_running_out_of_usage_posts_an_alert_with_the_reset(cr, monkeypatch):
    posted = []
    monkeypatch.setattr(cr, "RUNNER_NAME", "cloud-ec2-2")
    monkeypatch.setattr(cr, "_api", lambda m, p, b=None, **kw: (posted.append(b) or (201, {})))
    cr._notify_out_of_usage(["box-login:a44d94c3", "subscription-1"], [],
                            "2026-10-18T22:50:00Z", "turn-7d230f39")
    item = posted[0]["items"][0]
    assert item["kind"] == "claude_usage_exhausted" and item["level"] == "error"
    assert "out of Claude usage" in item["summary"]
    assert "2026-10-18T22:50:00Z" in item["summary"]
    assert item["key"] == "usage-exhausted:cloud-ec2-2"


def _said(events):
    return [e["payload"]["text"] for batch in events for e in batch if e.get("kind") == "assistant"]


def test_a_capped_credential_says_nothing_when_the_next_one_answers(cr, monkeypatch):
    """connect-kc-ace: ACE's Slack reply opened with "You've hit your weekly limit"
    and then the answer the next credential gave. The cap is the runner's to handle,
    not the turn's to say, unless every credential is out."""
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)

    def fake_once(prompt, turn_id, emit, **kw):
        if cr._claude_cred_label() == "subscription-1":
            emit([{"kind": "assistant", "payload": {"text": CAP_TEXT}}])
            return False, CAP_TEXT, ""
        emit([{"kind": "assistant", "payload": {"text": "No, I only read the playbook."}}])
        return True, "No, I only read the playbook.", "sess-1"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    events = []
    ok, _, _ = cr.execute_prompt("do it", "turn-abcdef12", events.append)
    assert ok is True
    assert _said(events) == ["No, I only read the playbook."]


def test_the_cap_is_shown_once_every_credential_is_out(cr, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_notify_out_of_usage", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_park_until_cap_resets", lambda *a, **k: "9pm")
    cr._apply_claude_credential(0)

    def fake_once(prompt, turn_id, emit, **kw):
        emit([{"kind": "assistant", "payload": {"text": CAP_TEXT}}])
        return False, CAP_TEXT, ""

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    events = []
    ok, _, _ = cr.execute_prompt("do it", "turn-abcdef12", events.append)
    assert ok is False
    assert _said(events) == [CAP_TEXT]


def test_an_agent_writing_about_limits_still_streams(cr, monkeypatch):
    """Only a short line can be a credential's refusal; the agent's own prose about
    a limit goes out as it is written, with whatever else it emitted alongside."""
    cr._apply_claude_credential(0)
    prose = ("The labs API caps exports, and a big opp has hit its row limit "
             "before. " * 6).strip()
    tool = {"kind": "tool_start", "payload": {"id": "t1", "name": "Bash", "input": {}}}

    def fake_once(prompt, turn_id, emit, **kw):
        emit([{"kind": "assistant", "payload": {"text": prose}}, tool])
        return True, prose, "sess-1"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    events = []
    cr.execute_prompt("do it", "turn-abcdef12", events.append)
    assert events[0] == [{"kind": "assistant", "payload": {"text": prose}}, tool]
