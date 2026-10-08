"""Threads: the stored frame (models.py) plus the messages DERIVED from tagged turns.

A message is a harness turn for the speaking agent tagged
``origin_ref.kind == "thread_message"`` with the thread id, its 1-based number
``n`` and the ``speaker``. Its words are the agent's REPLY BLOCK — a fenced
```thread JSON block it files as its close-out — read the way apps/huddles reads a
round reply: the message turn's own close-out (``report_summary``, where a cloud
runner's report lands), else a report-only close-out row the speaker filed (a
laptop session: ``canopy agent turn --session-id thread:<id>:<n>``), else the
turn's retained transcript.

Every read of a message goes through the caller's visible-turn queryset, and its
prompt and reply only when ``turn_access.can_read_turn_content`` allows it for
that turn — the same line ``/api/harness/turns/{id}/messages`` draws.
"""
from __future__ import annotations

import json
import re

from django.db.models import Exists, OuterRef

from apps.harness import services as hsvc
from apps.harness import turn_access
from apps.harness.models import Turn, TurnTranscript

from .models import AgentThread

MESSAGE_KIND = "thread_message"
#: A report-only close-out row's idempotency key (apps/agents/services.py). Such a
#: row may carry the message's origin_ref, but it is the REPLY to a message, never
#: a message of its own — so it never counts against the budget.
CLOSEOUT_PREFIX = "closeout:"
POSITIONS = ("agree", "counter", "decline", "question")

_FENCE = re.compile(r"```thread[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
#: A ```json or unlabelled fence — accepted when its object carries a "thread" key.
_LOOSE_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)


# ── the reply block ──────────────────────────────────────────────────────────

def _parse_obj(raw: str) -> tuple[dict | None, str]:
    try:
        b = json.loads(raw)
    except ValueError as e:
        return None, f"reply block is not valid JSON: {e}"
    if not isinstance(b, dict):
        return None, "reply block is not a JSON object"
    return b, ""


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _names(b: dict, thread: str, n: int) -> bool:
    return str(b.get("thread")) == thread and _int(b.get("n")) == n


def _loose_candidates(text: str) -> list[str]:
    out = list(reversed(_LOOSE_FENCE.findall(text)))
    whole = text.strip()
    if whole.startswith("{") and whole.endswith("}"):
        out.append(whole)
    return out


def extract_block(text: str, thread: str, n: int) -> tuple[dict | None, str]:
    """The LAST ```thread block in `text` naming this thread and message number.

    Same leniency as huddles' `extract_block`: a ```json / bare ``` fence, or the
    whole text as one JSON object, is accepted when it carries a "thread" key — but
    a labelled ```thread block always wins. Returns (block, "") on a match, else
    (None, why), where `why` is "" when there was no block at all."""
    text = text or ""
    err = ""
    for raw in reversed(_FENCE.findall(text)):
        b, perr = _parse_obj(raw)
        if b is None:
            err = err or perr
            continue
        if _names(b, thread, n):
            return b, ""
        err = err or f"block names thread {b.get('thread')!r} message {b.get('n')!r}"
    for raw in _loose_candidates(text):
        b, _ = _parse_obj(raw)
        if b is None or "thread" not in b:
            continue
        if _names(b, thread, n):
            return b, ""
        err = err or f"block names thread {b.get('thread')!r} message {b.get('n')!r}"
    return None, err


# ── counting and listing messages ────────────────────────────────────────────

def message_turns(thread_ids, qs=None):
    """Every message turn of these threads (any status), from `qs` (default: all
    turns — the budget is a fact about the thread, not about who is looking)."""
    qs = Turn.objects.all() if qs is None else qs
    return (qs.filter(origin_ref__kind=MESSAGE_KIND, origin_ref__thread__in=list(thread_ids))
            .exclude(idempotency_key__startswith=CLOSEOUT_PREFIX))


def messages_used(thread_id: str) -> int:
    return message_turns([thread_id]).count()


def used_counts(thread_ids) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref in message_turns(thread_ids).values_list("origin_ref", flat=True):
        tid = str((ref or {}).get("thread") or "")
        out[tid] = out.get(tid, 0) + 1
    return out


def _transcript_text(turn) -> str:
    try:
        msgs, _ = hsvc.transcript_messages(turn)
    except Exception:  # a broken or aged-out blob must not 500 the page
        return ""
    return "\n".join(m.get("plaintext") or "" for m in msgs if m.get("role") == "assistant")


def _ref(turn) -> dict:
    return turn.origin_ref if isinstance(turn.origin_ref, dict) else {}


def _message(turn, *, thread_id, user, closeouts, memo) -> dict:
    ref = _ref(turn)
    n, speaker = _int(ref.get("n")), str(ref.get("speaker") or "")
    readable = turn_access.can_read_turn_content(user, turn, memo)
    out = {"n": n, "speaker": speaker, "turn_id": str(turn.id), "status": turn.status,
           "created_at": turn.created_at, "finished_at": turn.finished_at,
           "content_hidden": not readable, "prompt": turn.prompt if readable else "",
           "block": None, "reply_source": "none", "reply_error": ""}
    if not readable:
        return out
    b, err = extract_block(turn.report_summary, thread_id, n)
    if b:
        return {**out, "block": b, "reply_source": "closeout"}
    for text in closeouts.get(speaker, ()):
        cb, cerr = extract_block(text, thread_id, n)
        if cb and str(cb.get("from") or speaker) == speaker:
            return {**out, "block": cb, "reply_source": "closeout"}
        err = err or cerr
    if getattr(turn, "has_transcript", False):
        tb, terr = extract_block(_transcript_text(turn), thread_id, n)
        if tb:
            return {**out, "block": tb, "reply_source": "transcript"}
        err = err or terr
    return {**out, "reply_error": err}


def messages(thread: AgentThread, *, user, visible_qs) -> list[dict]:
    turns = list(
        message_turns([thread.id], visible_qs)
        .annotate(has_transcript=Exists(TurnTranscript.objects.filter(turn=OuterRef("pk"))))
        .order_by("created_at")
    )
    speakers = {str(_ref(t).get("speaker") or "") for t in turns}
    closeouts: dict[str, list[str]] = {}
    if speakers:
        rows = (visible_qs.filter(agent__slug__in=speakers, report_summary__contains=thread.id,
                                  created_at__gte=thread.created_at)
                .order_by("-created_at").values_list("agent__slug", "report_summary"))
        for slug, text in rows:
            closeouts.setdefault(slug, []).append(text)
    memo: dict = {}
    out = [_message(t, thread_id=thread.id, user=user, closeouts=closeouts, memo=memo) for t in turns]
    return sorted(out, key=lambda m: (m["n"], m["created_at"]))


def out(thread: AgentThread, *, used: int, msgs: list[dict] | None = None) -> dict:
    return {
        "id": thread.id, "kind": thread.kind, "purpose": thread.purpose,
        "participants": thread.participants, "moderator": thread.moderator,
        "parent": thread.parent, "context": thread.context,
        "max_messages": thread.max_messages, "messages_used": used,
        "deadline_at": thread.deadline_at, "status": thread.status, "outcome": thread.outcome,
        "created_at": thread.created_at, "closed_at": thread.closed_at,
        "messages": msgs or [],
    }
