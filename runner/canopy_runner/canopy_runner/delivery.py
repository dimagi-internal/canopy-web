"""Did a message typed into a live session actually REACH Claude Code?

`cdp_control.open_and_send` types the text and presses Enter, and the sidecar
reports `sent` the moment the keystrokes are dispatched. Nothing checked that the
TUI took them. On 2026-10-02 (turn ffaa56ce, Hal's Slack thread) a message was
reported `sent`, never appeared in the session's transcript, and the turn sat
RUNNING with no reply until a human noticed. A lost message and a delivered one
were indistinguishable.

The transcript is the authority: Claude Code writes a `user` record when it takes
a prompt, or a `queue-operation` `enqueue` record when it is busy and queues it.
So: note the transcript's end BEFORE sending, then wait (bounded) for one of those
to appear.

Matching is deliberately two-tier, because the two failure directions cost very
different things:
  * a FALSE "not delivered" makes the human resend a message that did land — a
    duplicate in the agent's session;
  * a FALSE "delivered" is today's behaviour.
So a text match confirms, and — failing that — ANY new human-typed prompt in the
window also confirms (logged as unmatched). Claude Code may rewrite what was typed
(collapsed pastes, attachment paths turned into image blocks), and that must not
read as a loss. Only a window with NO new prompt at all is reported undelivered —
which is exactly the incident.
"""
from __future__ import annotations

import re
import time

CONFIRM_TIMEOUT = 20.0
CONFIRM_POLL = 0.25
PROBE_CHARS = 80
UNMATCHED_GRACE = 2.0

CONFIRMED, UNMATCHED, MISSING = "confirmed", "unmatched", "missing"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def probe_of(prompt: str) -> str:
    """The slice of `prompt` to look for: the start of its LONGEST line — the
    person's own words, not an attachment path or a leading blank — normalized and
    capped so a long message still matches a prefix."""
    lines = [ln for ln in (prompt or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return _norm(max(lines, key=len))[:PROBE_CHARS]


def _prompt_text(rec: dict) -> str | None:
    """The text of a human-typed prompt record, or None if `rec` is not one.

    Tool results, meta records and the agent's own output are not prompts."""
    kind = rec.get("type")
    if kind == "queue-operation":
        if rec.get("operation") != "enqueue":
            return None
        content = rec.get("content")
        return content if isinstance(content, str) else None
    if kind != "user" or rec.get("isMeta"):
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) if texts else ""
    return None


def classify(records: list[dict], prompt: str) -> str | None:
    """CONFIRMED if a new prompt record carries the probe, UNMATCHED if some new
    prompt record appeared but none carry it, None if no prompt record at all."""
    probe = probe_of(prompt)
    seen_prompt = False
    for rec in records:
        text = _prompt_text(rec)
        if text is None:
            continue
        seen_prompt = True
        if probe and probe in _norm(text):
            return CONFIRMED
    return UNMATCHED if seen_prompt else None


def confirm(reader, prompt: str, *, timeout: float | None = None,
            poll: float | None = None, clock=time.monotonic, sleep=time.sleep) -> str:
    """Wait for `prompt` to land in the transcript `reader` tails (already seeked to
    the pre-send end). Returns CONFIRMED, UNMATCHED or MISSING."""
    timeout = CONFIRM_TIMEOUT if timeout is None else timeout
    poll = CONFIRM_POLL if poll is None else poll
    deadline = clock() + timeout
    best: str | None = None
    while True:
        verdict = classify(reader.read_new(), prompt)
        if verdict == CONFIRMED:
            return CONFIRMED
        if verdict == UNMATCHED and best is None:
            # A prompt landed but not ours (yet): a short grace for the matching
            # record, not the full window — the delivery question is answered.
            best = UNMATCHED
            deadline = min(deadline, clock() + UNMATCHED_GRACE)
        if clock() >= deadline:
            return best or MISSING
        sleep(poll)
