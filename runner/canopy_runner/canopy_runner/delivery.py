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


# ── recovery: what to do about a send the transcript never showed ─────────────
#
# 2026-10-07: a Slack message to ACE was typed into a live session while Jonathan
# was using emdash, and nothing reached the transcript in the window. The turn
# failed with "Couldn't confirm…" — correct, but the message was very likely still
# recoverable: a click of his between the sidecar's focus and its insert sends the
# text elsewhere (nothing in the composer — retype it), and a swallowed Enter
# leaves it sitting in the composer (press Enter). Retyping is the one dangerous
# move — if the message is already in the session it lands twice (turn 22662f53
# typed one message four times) — so every branch below that could find the
# message already present says "don't retype", and anything it cannot read says
# "leave it".
#
# `recovery_action` is the pure decision over one look at the session (the
# sidecar's `composer` command); execute._recover_delivery acts on it.

SUBMIT, SEEN, RETYPE, LEAVE = "submit", "seen", "retype", "leave"

#: How much of the probe must appear on screen to count as "our message is up
#: there". Long enough not to match by accident, short enough to fit on the
#: screen when the TUI wraps or indents it (whitespace is squashed out anyway).
SCREEN_PROBE_CHARS = 60
#: A composer holding at least this much of our message (and nothing else) is
#: ours — shorter fragments could just as well be a human's word.
OURS_MIN_CHARS = 20

_PASTED = re.compile(r"^\[Pasted text #\d+[^\]]*\]$")
_PASTED_ANY = re.compile(r"\[Pasted text #\d+")
_PLACEHOLDER = re.compile(r"^(Try |Ask |/ for |\? for )", re.I)


def _squash(s: str) -> str:
    """Whitespace and box-drawing edges out: the screen wraps, indents and frames
    what was typed, and none of that is part of the words."""
    return re.sub(r"[\s│|]+", "", s or "")


def _collapses(prompt: str) -> bool:
    """Would claude show this as "[Pasted text #N +M lines]" instead of its text?
    A multi-line insert is read as a paste and collapsed."""
    return "\n" in (prompt or "").strip()


def composer_is_ours(typed: str, prompt: str) -> bool:
    """Does the composer hold OUR message, unsent, and nothing else?

    Deliberately strict in one direction: anything that might be a human's text is
    not ours, because pressing Enter on it sends their half-thought. Ours means the
    composer is (a prefix of / a slice of) our message, or the paste placeholder a
    multi-line message collapses to — the composer was empty when we typed, so a
    lone placeholder there can only be our paste."""
    typed = (typed or "").strip()
    if not typed or _PLACEHOLDER.match(typed):
        return False
    if _PASTED.match(typed):
        return _collapses(prompt)
    t, p = _squash(typed), _squash(prompt)
    if not t or not p:
        return False
    return t == p or (len(t) >= min(OURS_MIN_CHARS, len(p)) and t in p)


def seen_on_screen(screen: str, prompt: str) -> bool:
    """Is our message's text already rendered in the session (submitted, and the
    transcript just has not shown it)? Biased toward YES: a false yes costs a
    message the human re-sends; a false no risks typing it twice."""
    probe = _squash(probe_of(prompt))[:SCREEN_PROBE_CHARS]
    return bool(probe) and probe in _squash(screen)


def recovery_action(state: dict, prompt: str) -> tuple[str, str]:
    """(action, reason) for one look at the session after a MISSING confirm.

    SUBMIT  — our message is sitting unsent in the composer: press Enter. Pressing
              Enter on a composer holding only our text cannot send it twice.
    SEEN    — the composer is empty and our text is on screen: it was submitted;
              believe the screen and do not retype.
    RETYPE  — the composer is empty, the session is idle, and there is no trace of
              the message anywhere we can see: it went somewhere else (a stolen
              focus). The caller checks the transcript once more before retyping.
    LEAVE   — anything else: a human's text in the composer, no composer visible,
              a busy session we cannot read the state of, or a collapsed paste on
              screen that might be ours. Fail as before; a human looks.
    """
    if not state.get("found"):
        return LEAVE, "composer_not_visible"
    typed = (state.get("typed") or "").strip()
    if typed and not _PLACEHOLDER.match(typed):
        if composer_is_ours(typed, prompt):
            return SUBMIT, "composer_holds_message"
        return LEAVE, "composer_holds_other_text"
    screen = state.get("screen") or ""
    if seen_on_screen(screen, prompt):
        return SEEN, "message_on_screen"
    if state.get("running"):
        # Claude is working. Maybe on our message (the transcript we watch is not
        # the one it is writing) — retyping would queue a second copy behind it.
        return LEAVE, "session_busy"
    if _collapses(prompt) and _PASTED_ANY.search(screen):
        # Our message would show as "[Pasted text #N …]" and one is on screen; it
        # cannot be told apart from an earlier paste, so it might be ours.
        return LEAVE, "pasted_text_on_screen"
    return RETYPE, "no_trace"
