"""The dispatch-provenance protocol: canopy-web's end of a contract three repos share.

canopy stamps and strips (`orchestrator.agent_dispatch` / `agent_review`), Ada stamps the two
paths canopy's CLI cannot reach (`bin/_dispatch.py`), and canopy-web stamps `dispatch[]` and
delimits the decider's reply. None of the three can import the others.

Ada catches her own drift by RUNNING canopy's stamper and comparing output. canopy-web has no
canopy on its PATH, so these tests pin the exact bytes instead: changing one here is then a
deliberate, visible edit that must be made in all three repos together. A stamp the stripper
does not recognise suppresses NOTHING, silently — the whole failure the protocol prevents.
"""
import pytest

from apps.harness.dispatch_marker import (
    DISPATCH_MARKER,
    HUMAN_REPLY_CLOSE,
    HUMAN_REPLY_OPEN,
    SENDER_MARKER,
    stamp_dispatched,
    wrap_human_reply,
)


# ── the bytes ────────────────────────────────────────────────────────────────────────────────

def test_the_protocol_literals_are_exact():
    """Pinned against canopy `orchestrator.agent_dispatch` and ada `bin/_dispatch.py`. If you
    are changing one of these, change it in all three repos in the same sitting."""
    assert DISPATCH_MARKER == "<!-- canopy:dispatched-prompt -->"
    assert SENDER_MARKER == "<!-- canopy:dispatched-by={slug} -->"
    assert HUMAN_REPLY_OPEN == "<!-- canopy:human-reply -->"
    assert HUMAN_REPLY_CLOSE == "<!-- /canopy:human-reply -->"


def test_every_literal_is_an_inert_html_comment():
    """They must render as nothing and instruct the receiving agent to do nothing — a marker
    that reads like a directive would change the turn it is only supposed to label."""
    for lit in (DISPATCH_MARKER, SENDER_MARKER, HUMAN_REPLY_OPEN, HUMAN_REPLY_CLOSE):
        assert lit.startswith("<!--") and lit.endswith("-->")


def test_the_bare_marker_is_emitted_verbatim_whoever_sent_it():
    """THE back-compatibility contract. Every reader tests for this literal with `in`, including
    canopy installs that predate the sender comment. Fold the sender INTO the marker and those
    hosts stop recognising a stamped prompt — so briefs silently stop being suppressed there,
    which is the bug the marker exists to prevent, reintroduced where nobody would look."""
    for sender in ("ada", "hal", ""):
        assert DISPATCH_MARKER in stamp_dispatched("brief", sender=sender)


# ── stamping ─────────────────────────────────────────────────────────────────────────────────

def test_the_ask_comes_first():
    assert stamp_dispatched("FINDING: xyz", sender="ada").startswith("FINDING: xyz")


def test_the_receiving_agent_is_told_who_sent_it():
    out = stamp_dispatched("FINDING: xyz", sender="ada")
    assert "ada" in out.replace(DISPATCH_MARKER, "")
    assert "not typed by a human" in out


def test_the_provenance_carries_the_two_things_that_change_behaviour():
    """An agent that believes a human typed the brief skips the re-validation every brief asks
    for, and may read it as approval for an outbound action — a guardrail defeated by a machine."""
    out = stamp_dispatched("send the email", sender="ada")
    assert "VERIFY" in out and "re-validate" in out
    assert "NOT human approval" in out


def test_the_sender_is_named_in_its_own_comment():
    assert SENDER_MARKER.format(slug="ada") in stamp_dispatched("x", sender="ada")


def test_an_unknown_sender_degrades_rather_than_guesses():
    """Mislabelling who sent a brief is worse than not labelling it: canopy's outcome lens would
    hand one agent's report card to another."""
    out = stamp_dispatched("x")
    assert out.endswith(DISPATCH_MARKER)
    assert "another canopy agent" in out


def test_stamping_is_idempotent():
    """Ada stamps client-side before canopy-web ever sees the prompt (ada#55). Re-stamping must
    not double-mark it."""
    once = stamp_dispatched("brief", sender="ada")
    assert stamp_dispatched(once) == once
    assert stamp_dispatched(once, sender="hal") == once
    assert once.count(DISPATCH_MARKER) == 1


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_absent_prompt_is_left_alone(empty):
    """Absent means 'drain your board' — not a brief, so nothing to mislabel, and canopy's
    payload builder distinguishes absent from empty."""
    assert stamp_dispatched(empty) == empty


# ── the reply ────────────────────────────────────────────────────────────────────────────────

def test_a_wrapped_reply_is_recoverable_verbatim():
    said = "No, stop. Send it to eva instead."
    wrapped = wrap_human_reply(said)
    assert wrapped.startswith(HUMAN_REPLY_OPEN) and wrapped.endswith(HUMAN_REPLY_CLOSE)
    assert wrapped[len(HUMAN_REPLY_OPEN):-len(HUMAN_REPLY_CLOSE)] == said
