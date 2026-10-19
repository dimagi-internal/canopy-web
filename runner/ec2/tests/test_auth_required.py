"""A dead subscription token is not a capped one, and neither is a plain bug.

The runner already knew about usage caps. It did not know about the state a
subscription actually spends most of its downtime in: the token has expired and
a human must sign in again. With no such concept, `claude`'s "Please run /login"
read as an ordinary turn failure — so the harness re-queued the turn, the same
dead token failed it again, and nothing anywhere said the box needed a person.

Every string asserted below was read out of the shipped `claude` binary
(2.1.263) rather than guessed, because a hand-transcribed copy of another
product's user-facing copy is exactly how the session cap went unnoticed.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def cr(cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-1")
    return cloud_runner


# ── the cap the fleet actually hit, and kept hitting ────────────────────────

def test_the_session_cap_is_a_usage_cap(cr):
    """The 5-hour cap — by far the most frequently hit, and the one the original
    marker list missed. Observed on cloud-ec2-1 on 2026-09-08, where it re-queued
    a turn three times against the same capped credential."""
    assert cr._is_usage_cap("You've hit your session limit · resets 4:20pm (UTC)")


@pytest.mark.parametrize("text", [
    "You've hit your session limit · resets 4:20pm (UTC)",
    "You've hit your weekly limit · resets Aug 3, 11pm (UTC)",
    "You've hit your daily limit · resets midnight (UTC)",
    "you have reached your weekly usage limit",
    "you have reached your daily usage limit",
    "Error: usage limit reached",
])
def test_every_cap_wording_matches_by_shape(cr, text):
    """Session, daily and weekly caps are each worded differently and the set
    grows. Enumerating phrasings is what missed the session cap, so the match is
    on the shape every cap message shares: a hit/reached verb, then "limit"."""
    assert cr._is_usage_cap(text)


def test_the_capless_phrasings_still_match(cr):
    """The few that never say "limit" — the shape rule cannot see these."""
    assert cr._is_usage_cap("your credit balance is too low")
    assert cr._is_usage_cap("rate limit exceeded")


# ── the state that had no name ──────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Not logged in",
    "Please run /login",
    "Please run /login and sign in with your Claude.ai account (not Console).",
    "API Error: 401 Invalid API key · Please run /login",
    "Auth token expired or invalid",
    "Refresh token expired",
])
def test_a_dead_token_is_auth_required(cr, text):
    assert cr._is_auth_required(text)


def test_a_cap_is_not_auth_required(cr):
    """The distinction that earns this its own state: a cap fixes itself when the
    clock runs out, a dead token never does. Confusing them means either waiting
    forever for a reset that is not coming, or paging a human about a cap."""
    assert not cr._is_auth_required("You've hit your session limit · resets 4:20pm (UTC)")
    assert not cr._is_auth_required("You've hit your weekly limit · resets Aug 3, 11pm (UTC)")


def test_an_ordinary_failure_is_neither(cr):
    """The expensive false positive, in both directions."""
    assert not cr._is_auth_required("TypeError: 'NoneType' object is not subscriptable")
    assert not cr._is_auth_required("")
    assert not cr._is_usage_cap("TypeError: 'NoneType' object is not subscriptable")


def test_a_login_mention_in_prose_is_not_a_dead_token(cr):
    """An agent that merely WRITES about logging in must not mark the box dead —
    turn text is agent output, so the markers have to be the CLI's own wording."""
    assert not cr._is_auth_required(
        "I updated the docs to explain how a new operator runs /login on their laptop."
    )


def test_a_limit_mentioned_in_passing_is_not_a_cap(cr):
    """The cost of matching by shape. Both of these appear in ordinary agent
    prose, and neither is Anthropic telling us to stop — "limit" alone must not
    be enough, which is why the verb has to be near it."""
    assert not cr._is_usage_cap("The slug is truncated at Connect's 50-char limit.")
    assert not cr._is_usage_cap("I raised the limit in the config and re-ran it.")


# ── the ACP executor's wording (investigation 5) ────────────────────────────

@pytest.mark.parametrize("text", [
    "runner error (acp): Authentication required",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
])
def test_an_expired_login_under_acp_is_auth_required(cr, text):
    """cloud-ec2-2, 2026-10-15 (turn 9fbe2240): the box's only claude.ai login
    could not refresh. The agent's last line was the second string, and the turn
    failed with the first. Neither matched, so nobody was told the box needed a
    person, and the note never said so either."""
    assert cr._is_auth_required(text)
    assert not cr._is_usage_cap(text)


def test_authentication_required_in_prose_is_not_a_dead_token(cr):
    """Only the runner's own ACP note counts. An agent describing an API that
    answers "authentication required" must not mark the box dead."""
    assert not cr._is_auth_required(
        "The HQ endpoint returned 401 Authentication required, so I used the API key.")


# ── a lone credential's bare ACP refusal is probed, not assumed dead ────────

CAP_LINE = "You've hit your weekly limit · resets 11pm (UTC)"


@pytest.fixture()
def lone(cr, monkeypatch):
    """One setup-token, which ACP refused with its bare `auth_required`."""
    import time as _time
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid-1")
    monkeypatch.setattr(cr, "_reload_claude_credentials", lambda: False)
    seen = {"auth": [], "usage": [], "park": [], "probed": 0}
    monkeypatch.setattr(cr, "_notify_auth_required", lambda label, tid: seen["auth"].append(label))
    monkeypatch.setattr(cr, "_notify_out_of_usage",
                        lambda capped, dead, until, tid: seen["usage"].append((capped, dead)))

    def park(resets, text, tid):
        seen["park"].append(list(resets))
        return "2026-10-18T23:00:00Z"

    monkeypatch.setattr(cr, "_park_until_cap_resets", park)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (
        False, "runner error (acp): Authentication required", ""))

    def with_probe(status, detail="", reset=None):
        def probe(index):
            seen["probed"] += 1
            return {"label": cr._CLAUDE_CREDS[index][0], "status": status, "detail": detail,
                    "reset": reset, "at": _time.time()}
        monkeypatch.setattr(cr, "_probe_credential", probe)

    cr._apply_claude_credential(0)
    return cr, seen, with_probe


def test_a_lone_capped_token_over_acp_is_a_cap(lone):
    """cloud-ec2-fork, 2026-10-18: one setup-token at its weekly cap, and every
    turn said "not signed in", because nothing else in the turn had capped first."""
    import time as _time
    cr, seen, with_probe = lone
    with_probe("capped", CAP_LINE, reset=_time.time() + 3600)
    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert ok is False
    assert seen["auth"] == []
    assert seen["usage"] == [(["subscription-1"], [])]
    assert seen["park"] and seen["park"][0]
    assert "out of Claude usage" in text and "weekly limit" in text
    assert "not signed in" not in text
    assert cr._PROBES["subscription-1"]["status"] == "capped"


def test_a_lone_dead_token_over_acp_stays_dead(lone):
    cr, seen, with_probe = lone
    with_probe("dead", "Not logged in · Please run /login")
    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert seen["auth"] == ["subscription-1"]
    assert "not signed in" in text
    assert seen["usage"] == [] and seen["park"] == []


def test_probe_unknown_keeps_todays_dead_path(lone):
    cr, seen, with_probe = lone
    with_probe("unknown", "no answer within 90s")
    ok, text, _ = cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert seen["auth"] == ["subscription-1"]
    assert "this box is not signed in" in text
    # An inconclusive probe is not evidence the credential is dead.
    assert cr._PROBES["subscription-1"]["status"] == "unknown"


def test_cli_sentence_needs_no_probe(lone, monkeypatch):
    cr, seen, with_probe = lone
    with_probe("capped", CAP_LINE)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, "Not logged in", ""))
    cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert seen["probed"] == 0
    assert seen["auth"] == ["subscription-1"]
    assert cr._PROBES["subscription-1"]["status"] == "dead"
