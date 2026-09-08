"""Parsing `claude setup-token`'s terminal output.

The fixture is a REAL capture from `claude` 2.1.263 driven under a pty (PKCE
nonces redacted; the run was killed before any code was entered, so nothing was
minted). It is the fixture and not a hand-written string on purpose: the exact
bytes — OSC-8 hyperlink wrappers, spinner frames, a URL wrapped mid-token at the
terminal width — are what the parser has to survive, and none of them are
guessable from the outside.

These live in cloud_runner.py rather than a module of their own: the deploy
ships that file ALONE, so a sibling module is never delivered to a box.
"""
from __future__ import annotations

import pathlib

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_FIXTURE = _HERE / "fixtures" / "setup_token_pty.bin"


@pytest.fixture()
def cm(cloud_runner):
    return cloud_runner


@pytest.fixture()
def raw() -> bytes:
    return _FIXTURE.read_bytes()


def test_the_authorize_url_is_recovered_from_a_real_capture(cm, raw):
    url = cm.extract_authorize_url(raw)
    assert url is not None, "no URL found — the TUI's rendering has changed"
    assert url.startswith("https://claude.com/cai/oauth/authorize?")
    # The parameters that make it a usable PKCE authorize link. A URL missing any
    # of these renders an error page for the human rather than a sign-in.
    for param in ("client_id=", "response_type=code", "redirect_uri=",
                  "code_challenge=", "code_challenge_method=S256", "state="):
        assert param in url, f"{param} missing from the recovered URL"


def test_the_url_carries_no_terminal_control_bytes(cm, raw):
    """The URL lives INSIDE an OSC-8 escape, so a careless strip either drops it
    or returns it with control bytes still embedded — which fails only later, in
    a browser, as an invalid request."""
    url = cm.extract_authorize_url(raw)
    assert "\x1b" not in url and "\x07" not in url
    assert "\n" not in url and " " not in url


def test_the_wrapped_copy_never_wins(cm, raw):
    """The TUI prints the URL twice and wraps the visible copy at the terminal
    width. Picking the short one yields a truncated link that looks fine."""
    url = cm.extract_authorize_url(raw)
    assert url.rstrip("\x00").endswith(("REDACTED_STATE",)), (
        "recovered the truncated copy — the full URL ends at the state param")


def test_no_url_before_it_renders(cm):
    """`start()` polls, so 'not yet' must be distinguishable from 'never'."""
    assert cm.extract_authorize_url(b"") is None
    assert cm.extract_authorize_url(b"Welcome to Claude Code v2.1.263\r\n") is None


def test_a_claude_ai_url_is_not_the_authorize_url(cm):
    """The binary also contains `https://claude.ai/oauth/...` strings. Matching
    those finds nothing at runtime, and matching them LOOSELY would return a
    link that is not the flow we started."""
    assert cm.extract_authorize_url(b"see https://claude.ai/oauth/claude for help") is None


# ── the token half ─────────────────────────────────────────────────────────

def test_the_minted_token_is_recovered(cm):
    out = b"Success! Your token:\r\n\x1b[32msk-ant-oat01-AbC_dEf-123\x1b[0m\r\n"
    assert cm.extract_token(out) == "sk-ant-oat01-AbC_dEf-123"


def test_an_api_key_is_not_a_setup_token(cm):
    """`sk-ant-api…` and `sk-ant-admin…` are different credentials that would be
    staged into the wrong env var and fail confusingly."""
    assert cm.extract_token(b"sk-ant-api03-nope") is None
    assert cm.extract_token(b"sk-ant-admin01-nope") is None


def test_no_token_while_the_flow_is_still_running(cm, raw):
    """The authorize-URL screen must not read as a completed mint."""
    assert cm.extract_token(raw) is None
