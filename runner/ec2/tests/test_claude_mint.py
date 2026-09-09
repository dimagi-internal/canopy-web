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


# ── the truncation that shipped, and why the fixture missed it ─────────────
#
# The first live run stored an 80-character URL — exactly the terminal width.
# `_pump` returns on the FIRST match, and on a real box the visible copy's first
# WRAPPED line lands in the buffer before the OSC-8 payload carrying the whole
# URL does. The human got "Invalid OAuth Request / Missing redirect_uri".
#
# The fixture could not catch it: it holds the COMPLETE output, and the parser
# was only ever asked about a finished buffer. A stream is not a buffer.

_TRUNCATED = (
    b"https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88"
)


def test_a_wrapped_first_line_is_not_an_answer(cm):
    """80 chars, cut mid-client_id. It parses as a URL and is useless as one."""
    assert cm.extract_authorize_url(_TRUNCATED) is None


def test_a_url_missing_any_required_parameter_is_rejected(cm, raw):
    """Acceptance is semantic, not "does it look like a URL" — the flow needs
    every one of these, and a partial read can drop any of them."""
    full = cm.extract_authorize_url(raw)
    assert full is not None
    for param in ("client_id", "response_type", "redirect_uri",
                  "code_challenge", "code_challenge_method", "state"):
        assert f"{param}=" in full
        # Chop the URL just before this parameter and it must stop being valid.
        assert cm.extract_authorize_url(full[:full.index(f"{param}=")].encode()) is None


def test_the_url_is_recovered_progressively_not_just_from_a_finished_buffer(cm, raw):
    """Feed the capture one chunk at a time, as a pty actually delivers it. The
    parser must answer None until the whole URL is present, then the full one —
    never a prefix of it."""
    seen = None
    for end in range(0, len(raw) + 1, 64):
        got = cm.extract_authorize_url(raw[:end])
        if got is not None:
            seen = got
            for param in ("redirect_uri", "code_challenge_method", "state"):
                assert f"{param}=" in got, f"answered a URL missing {param}"
    assert seen == cm.extract_authorize_url(raw)


# ── reporting what the CLI actually said ───────────────────────────────────
#
# The first two live failures were debugged by GUESSING — staleness, then a
# mismatched session — and both theories were wrong. The runner knew exactly
# what `setup-token` had printed and discarded it, reporting only that no token
# appeared. These pin the fix: say what it said.

def test_the_failure_reports_the_clis_own_words(cm):
    out = b"\x1b[2mPasting code\x1b[0m\r\nInvalid authorization code. Please try again.\r\n"
    assert "Invalid authorization code" in cm._diagnostic_tail(out)


def test_the_tail_never_carries_a_token(cm):
    """Diagnostic output is shown to a human on a web page — it is not a place
    to spill the credential we just minted."""
    out = b"Success! Your token:\r\nsk-ant-oat01-SECRETVALUE\r\ndone\r\n"
    tail = cm._diagnostic_tail(out)
    assert "sk-ant-oat01-SECRETVALUE" not in tail
    assert "<redacted>" in tail


def test_spinner_frames_do_not_crowd_out_the_message(cm):
    """A TUI redraws constantly; without de-duplication the tail is all spinner
    and none of the sentence that matters."""
    noise = b"".join(b"\r\n" + f.encode() for f in "✦✳✶✻✽" * 40)
    out = noise + b"\r\nCode expired. Start a new sign-in.\r\n"
    tail = cm._diagnostic_tail(out)
    assert "Code expired" in tail


def test_the_tail_is_bounded(cm, raw):
    """It rides in a failure detail that a human reads, not a log."""
    assert len(cm._diagnostic_tail(raw)) <= 600


# ── typing the code in ─────────────────────────────────────────────────────
#
# Sent as ONE chunk (`code + b"\r"`), an older CLI reads the arrival as a paste
# and keeps the carriage return as the last CHARACTER of the pasted text rather
# than acting on it. Nothing submits and the CLI says nothing, so the pump burns
# its full 90s and reports "never printed a token" for what is really "never
# asked". Measured against a live `setup-token` on claude 2.1.197: one write is
# silent, code-settle-CR answers in 0.6s, code-settle-LF is silent. Newer CLIs
# (2.1.266) submit either way — which is exactly why this was invisible in
# development and cost a human four sign-in attempts.

def _fake_submit(cm, monkeypatch, code="THECODE#THESTATE"):
    """Drive submit_code with the pty and the pump stubbed, recording the
    ordered sequence of writes and sleeps it performs."""
    events: list = []
    monkeypatch.setattr(cm.os, "write",
                        lambda fd, data: (events.append(data), len(data))[1])
    monkeypatch.setattr(cm.time, "sleep", lambda s: events.append(("sleep", s)))
    session = cm.MintSession()
    session._fd = 7
    monkeypatch.setattr(session, "_pump", lambda timeout, extract: "sk-ant-oat01-ok")
    monkeypatch.setattr(session, "close", lambda: None)
    token = session.submit_code(code)
    return token, events


def test_the_return_is_a_keypress_of_its_own_not_the_tail_of_the_paste(cm, monkeypatch):
    token, events = _fake_submit(cm, monkeypatch)
    assert token == "sk-ant-oat01-ok"
    assert events[0] == b"THECODE#THESTATE", "the code must go in without a return attached"
    assert events[-1] == b"\r", "Enter must arrive as its own write"
    assert not any(isinstance(e, bytes) and e.endswith(b"\r") and len(e) > 1
                   for e in events), "a code with the return glued on is the bug"


def test_the_paste_is_allowed_to_settle_before_enter(cm, monkeypatch):
    """Without a pause the two writes can still coalesce into one read on the
    CLI's side, which is the same failure with extra steps."""
    _, events = _fake_submit(cm, monkeypatch)
    kinds = [("sleep" if isinstance(e, tuple) else "write") for e in events]
    assert kinds == ["write", "sleep", "write"], kinds
    assert events[1][1] > 0, "a zero settle is not a settle"


def test_the_code_is_stripped_before_it_is_typed(cm, monkeypatch):
    """A code arrives from a browser field; trailing whitespace is the human's,
    not the credential's."""
    _, events = _fake_submit(cm, monkeypatch, code="  THECODE#THESTATE\n ")
    assert events[0] == b"THECODE#THESTATE"


def test_the_verdict_survives_the_length_cap(cm):
    """The LAST thing the CLI said is the reason this function exists, so the
    cap must eat the oldest lines, never the newest.

    Regression: the tail was joined oldest-first and then sliced `[:limit]`,
    which trims the end — the newest line. On 2026-09-08 a real failure detail
    ended with the single character "O", the decapitated head of "OAuth error:
    Request failed with status code 400", and was read as "the CLI printed
    nothing at all"."""
    noise = b"".join(b"\r\n" + f"chatter line {i} that is here only to fill the budget".encode()
                     for i in range(40))
    out = noise + b"\r\nOAuth error: Request failed with status code 400\r\n"
    tail = cm._diagnostic_tail(out)
    assert len(tail) <= 600
    assert tail.endswith("OAuth error: Request failed with status code 400"), tail[-120:]


def test_a_single_over_long_line_is_still_reported(cm):
    """One line longer than the whole budget must not collapse to nothing —
    and its HEAD is the informative half."""
    out = b"\r\n" + b"Invalid authorization code. " + b"x" * 4000 + b"\r\n"
    tail = cm._diagnostic_tail(out)
    assert len(tail) <= 600
    assert tail.startswith("Invalid authorization code.")


# ── the token's format is Anthropic's to choose, not ours to pin ───────────
#
# 2026-09-08, live: a sign-in COMPLETED. The CLI printed "Long-lived
# authentication token created successfully!" and rendered the credential. The
# runner discarded it and reported "setup-token never printed a token", because
# `\bsk-ant-oat…` did not match. The token is returned by Anthropic's token
# endpoint and merely displayed by the CLI, so pinning the prefix bound this
# runner to a taxonomy nobody promised us — and cost a human a real sign-in.

#: The success screen as `strip_terminal` renders it. Spaces are largely absent
#: on purpose: the TUI positions text with cursor moves, which the CSI strip
#: removes, so words glue together. Anything matching the banner must tolerate it.
_SUCCESS = (
    "Pastecodehereifprompted>\n"
    "*********************nAa-r0\n"
    "✓Long-livedauthenticationtokencreatedsuccessfully!\n"
    "YourOAuthtoken(validfor1year):\n"
    "{token}\n"
    "Storethistokensecurely.Youwon'tbeabletoseeitagain.\n"
)


def test_a_token_the_cli_announces_is_taken_whatever_its_prefix(cm):
    """The regression. A prefix we have never seen is still the credential the
    human just created, and throwing it away is the worst outcome available."""
    out = _SUCCESS.format(token="sk-ant-zzz99-QqWwEe_rTtYy-0123456789abcdef").encode()
    assert cm.extract_token(out) == "sk-ant-zzz99-QqWwEe_rTtYy-0123456789abcdef"


def test_the_known_format_still_wins_when_present(cm):
    out = _SUCCESS.format(token="sk-ant-oat01-AbC_dEf-0123456789abcdefghij").encode()
    assert cm.extract_token(out) == "sk-ant-oat01-AbC_dEf-0123456789abcdefghij"


def test_the_banner_is_matched_through_the_tuis_missing_spaces(cm):
    """`YourOAuthtoken(validfor1year):` — not a typo, that is what the terminal
    strip actually yields."""
    assert cm._TOKEN_BANNER.search("YourOAuthtoken(validfor1year):\nsk-ant-x-0123456789abcdef")


def test_an_unannounced_api_key_is_still_refused(cm):
    """Loosening the prefix must not start staging the wrong credential."""
    assert cm.extract_token(b"sk-ant-api03-nope0123456789abcdefghij") is None
    assert cm.extract_token(b"sk-ant-admin01-nope0123456789abcdefgh") is None


# ── a created token we cannot read is NOT a missing token ──────────────────
#
# 2026-09-08: a human signed in correctly, Claude issued a year-long token, the
# runner failed to match its prefix, and the operator was told "setup-token
# never printed a token". That sentence pointed the debugging at the sign-in —
# which had worked perfectly — instead of at the parser, and cost a second
# attempt to notice. The two faults need opposite responses, so they get
# different exceptions.

def _submit_expecting(cm, monkeypatch, buf: bytes):
    monkeypatch.setattr(cm.os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    session = cm.MintSession()
    session._fd = 7
    session._buf = buf
    monkeypatch.setattr(session, "_pump", lambda timeout, extract: extract(buf))
    monkeypatch.setattr(session, "close", lambda: None)
    with pytest.raises(cm.MintTimeout) as exc:
        session.submit_code("THECODE#THESTATE")
    return exc.value


def test_an_unreadable_credential_is_reported_as_its_own_fault(cm, monkeypatch):
    """The CLI announced success; extraction found nothing it recognised."""
    buf = ("YourOAuthtoken(validfor1year):\n"
           "totally-unrecognisable-credential-value\n").encode()
    err = _submit_expecting(cm, monkeypatch, buf)
    assert isinstance(err, cm.MintUnreadableToken)
    assert "SUCCEEDED" in str(err)
    assert "bug in the runner" in str(err)


def test_a_genuine_failure_is_still_a_plain_timeout(cm, monkeypatch):
    """Nothing was announced, so a new attempt is the right response."""
    buf = b"Pastecodehereifprompted>\n****************\n"
    err = _submit_expecting(cm, monkeypatch, buf)
    assert not isinstance(err, cm.MintUnreadableToken)
    assert "never printed a token" in str(err)


def test_the_banner_outranks_our_prefix_opinion(cm):
    """DELIBERATE, and load-bearing: under the CLI's own "Your OAuth token"
    label the value IS the token, so pass 1 does not apply pass 3's api/admin
    exclusion. Applying it there would re-create the 2026-09-08 bug — our
    taxonomy overruling the tool that owns the credential. Documented as a test
    so it is not "tidied up" into a regression."""
    announced = b"YourOAuthtoken(validfor1year):\nsk-ant-api03-0123456789abcdefghij\n"
    assert cm.extract_token(announced) == "sk-ant-api03-0123456789abcdefghij"
    # ...but unanchored, the same string is refused.
    assert cm.extract_token(b"sk-ant-api03-0123456789abcdefghij") is None
