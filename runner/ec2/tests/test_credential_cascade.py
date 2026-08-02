"""The Claude credential cascade: subscription-1 -> subscription-2 -> API key.

A subscription's weekly cap takes out EVERY agent on the box at once, and an
unattended agent has nobody to tell (2026-08-01: the whole fleet sat dead on one
exhausted login). These tests pin the behaviour that fixes it, and — just as
important — the behaviour that must NOT happen: an ordinary turn failure being
re-run against every credential in turn, spending real money on a plain bug.
"""
from __future__ import annotations

import pytest

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


def test_retry_drops_resume_so_a_partial_session_is_not_reused(cr, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    resumes = []

    def fake_once(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        resumes.append(resume_session_id)
        if len(resumes) == 1:
            return False, CAP_TEXT, ""
        return True, "ok", "s"

    monkeypatch.setattr(cr, "_execute_once", fake_once)
    cr.execute_prompt("p", "turn-abcdef12", lambda e: None, resume_session_id="sess-old")
    assert resumes == ["sess-old", None]


# --- mid-run reload: rescuing a box without restarting it --------------------
def test_exhausted_cascade_rereads_credentials_before_giving_up(cr, monkeypatch):
    """Credentials are staged into the process at start-up, so an operator who
    rescues a stuck box with `canopy runner credential` would otherwise have to
    restart the service for the fix to land."""
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._CLAUDE_CREDS.clear()
    cr._CLAUDE_CREDS.append(("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "old"))
    cr._apply_claude_credential(0)
    cr._CLAUDE_CRED_RUNNER_ID = "r-1"

    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {"claude_token": "rescued"}))
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
    cr._CLAUDE_CREDS.append(("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "same"))
    cr._apply_claude_credential(0)
    cr._CLAUDE_CRED_RUNNER_ID = "r-1"
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {"claude_token": "same"}))
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, CAP_TEXT, ""))
    ok, text, _ = cr.execute_prompt("p", "turn-abcdef12", lambda e: None)
    assert ok is False
    assert "exhausted" in text
