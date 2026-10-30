"""The note a runner stores when it finishes a turn, and where to read the rest.

A turn's `result_note` is its final message, kept to `MAX_CHARS`. It used to be a
bare `text[:2000]`, so a long reply simply stopped mid-sentence with nothing to
say so. A caller reading it through `canopy agent turns` could not tell a cut
reply from a short one, and dispatched a follow-up to get the rest, which started
a new session with no memory of the first (canopy-web#1955: 12 of 17 notes were
exactly 2,000 characters). A cut note now ends with a marker that says it was cut
and links the page that holds the whole reply, within the same cap.

Shared by both runners (cloud_runner, the laptop runner's desktop runtime).
Stdlib only, like the rest of this package.
"""
from __future__ import annotations

from .closeout import find as find_closeout

#: The longest note a runner sends. Unchanged from the old `[:2000]`.
MAX_CHARS = 2000

ELLIPSIS = "…"


def turn_url(base: str, turn: dict) -> str:
    """Where a person can read this turn's whole reply on canopy-web, from the
    claimed-turn payload. Mirrors `apps.harness.auto_debug.turn_page_url`: a chat
    turn lives in its chat, an agent turn on the agent's Turns tab (anchored to
    its card, which shows the retained transcript), anything else in the
    workspace's activity log. "" when there is no base to build on."""
    base = (base or "").rstrip("/")
    if not base:
        return ""
    ws = turn.get("workspace_slug") or ""
    chat = (turn.get("origin_ref") or {}).get("chat_session_id") or ""
    agent = turn.get("agent_slug") or ""
    turn_id = str(turn.get("id") or "")
    if chat and ws:
        return f"{base}/w/{ws}/chat/{chat}"
    if agent and ws:
        anchor = f"#{turn_id}" if turn_id else ""
        return f"{base}/w/{ws}/agents/{agent}/turns{anchor}"
    if ws:
        return f"{base}/w/{ws}/activity"
    return f"{base}/activity"


def cap(text: str, url: str = "", limit: int = MAX_CHARS) -> str:
    """`text` unchanged when it fits in `limit`; otherwise cut so that it, plus a
    marker naming the cut and `url`, fits in `limit` exactly or less.

    A closeout status line (`[closeout: <VERDICT>]`, canopy_transcript.closeout) is the
    LAST line of the reply and the one part of it a reader of the note acts on, so a cut
    keeps it: the head is shortened instead, and the line follows the marker. The same
    goes for the runner's own reason a turn failed (`_runner_reason`), which wins over
    a closeout line because it comes after it."""
    text = text or ""
    if len(text) <= limit:
        return text
    marker = (f" {ELLIPSIS} [truncated — full reply: {url}]" if url
              else f" {ELLIPSIS} [truncated]")
    if len(marker) >= limit:  # a cap too small for the marker: the marker alone
        return marker.strip()[:limit]
    tail = _runner_reason(text, limit // 2 - len(marker) - 1)
    if not tail:
        _, line = find_closeout(text)
        tail = f"\n\n{line}" if line and text.rstrip().endswith(line) else ""
    if tail and len(marker) + len(tail) < limit // 2:
        return text[: limit - len(marker) - len(tail)].rstrip() + marker + tail
    return text[: limit - len(marker)].rstrip() + marker


#: How the runner opens the paragraph it appends to a reply to say why the turn
#: failed (cloud_runner: "[runner] out of Claude usage — …", "[runner] this box is
#: not signed in …").
RUNNER_PREFIX = "[runner] "


def _runner_reason(text: str, room: int) -> str:
    """The runner's closing `[runner] …` paragraph, as the tail a cut keeps ("" when
    the reply does not end with one). Cut to `room` if it is longer.

    Failure investigation 21 (turn c4dd7019, ACE in a chat on cloud-ec2-live,
    2026-10-30): every Claude login on the box hit its cap mid-turn, and the runner
    appended its "[runner] out of Claude usage" paragraph to ACE's long partial
    reply. The cut kept the head of the reply and dropped that paragraph, so the
    failure read as ACE narrating its work, and the debugger had no cause to go on."""
    at = text.rfind("\n\n" + RUNNER_PREFIX)
    if at < 0 or room <= 0:
        return ""
    tail = text[at:].rstrip()
    if "\n\n" in tail[2:]:
        return ""  # a mention earlier in the reply, not the paragraph that ends it
    if len(tail) > room:
        tail = tail[: room - 1].rstrip() + ELLIPSIS
    return tail
