"""The drill prompt must not forbid the one thing it then requires.

Three of five agents drilled on the rebuilt cloud box (2026-08-12) ran every check,
passed, and never reported — so the grid read `pending` forever and the box looked
unreachable when it was fine. hal said why in its turn note: "Drill checks all
passed, but the final callback needs a flag."

The prompt caused it. Step 2 said "take NO outward action — no emails, no posts,
no board writes, no state mutations anywhere", and step 3 then asked for a POST.
Every agent in this fleet runs under a hard guardrail that outbound actions need
human approval, so the honest reading of those two lines together is "stop and
ask" — and on an unattended drill there is nobody to ask.

A prompt that contradicts itself gets resolved by the model, not by us, so these
assert the contradiction is gone and the exemption is explicit.
"""
from __future__ import annotations

import re

from apps.harness.services import DRILL_PROMPT


def _prompt() -> str:
    return DRILL_PROMPT.format(agent_slug="eva", report_url="https://example.test/report")


def test_the_report_is_named_as_exempt_from_the_read_only_rule():
    """The no-outward-action line must carry its own exception, in the same breath.
    An exemption stated three paragraphs later loses to the prohibition."""
    text = _prompt()
    para = text[text.index("take NO outward action"):text.index("3.")]
    assert "exception" in para.lower(), (
        "the read-only prohibition does not name the report as an exception:\n" + para)


def test_the_report_is_marked_mandatory_and_pre_authorized():
    text = _prompt().lower()
    assert "mandatory" in text
    assert "authorized" in text or "authorised" in text


def test_the_prompt_forbids_stopping_to_ask_for_approval():
    """The precise failure mode: agents treated the callback as an outbound action
    needing a human, on a turn with no human attached."""
    text = _prompt().lower()
    assert "nobody" in text or "no one" in text, "the prompt never says nobody is there to ask"
    assert re.search(r"do not stop|don't stop|do not (?:pause|wait|request)", text), text


def test_a_failing_check_must_still_be_reported():
    """Reporting a bad result is the drill working. An agent that suppresses a
    failure it could not fix produces the same silence as a dead box."""
    text = _prompt().lower()
    assert "including a failure" in text or "report whatever you found" in text


def test_the_report_call_is_still_present_and_addressed():
    text = _prompt()
    assert "https://example.test/report" in text
    assert "curl -s -X POST" in text
    assert '"outcome": "pass"' in text and '"fail"' in text


def test_everything_else_is_still_read_only():
    """The fix must not become a licence to act — only the report is exempted."""
    text = _prompt()
    assert "READ-ONLY" in text
    assert "no emails" in text and "no board writes" in text
