"""Agent replies go back to the Slack thread they were asked in.

A Slack-born session is a normal chat session, so its turns produce the same
ledger rows canopy-web renders: every chat turn's reply lands as `assistant`
TurnEvents (for transcript-sourced sessions too — `project_events` skips them
because the transcript is their durable record, but the ledger still carries
the bridged reply). This module is a third consumer of `turn_events_appended`,
beside the realtime fan-out and the Message projection, and posts what it sees
into the thread.

Deliberately a mirror of what the WEB shows, not a second channel with its own
rules: prose the agent wrote goes to the thread; tool calls, heartbeats and
status do not, except a turn that FAILED — silence there would read exactly
like the bug that started all this (a reply that never came).
"""
from __future__ import annotations

import logging
import re

from django.db import IntegrityError

from . import client
from .models import SlackRelayPost

logger = logging.getLogger(__name__)

#: Slack renders up to 40k characters but truncates long messages in the
#: client and splits them unpredictably; 3500 keeps each post whole.
MAX_POST_CHARS = 3500


def to_mrkdwn(text: str) -> str:
    """The Markdown an agent writes, in the dialect Slack renders.

    Only what agents actually emit and what Slack gets visibly wrong: bold
    (`**x**` would show literal asterisks), links (`[t](u)` would show the
    brackets), headings (`# x` shows the hash). Code fences, inline code,
    bullets and plain URLs already render. Code spans are left untouched so a
    `**kwargs` in a snippet survives.
    """
    parts = re.split(r"(```.*?```|`[^`\n]*`)", text or "", flags=re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2:          # a code span or fence — verbatim
            out.append(part)
            continue
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"__(.+?)__", r"*\1*", part)
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", part)
        out.append(part)
    return "".join(out)


def split(text: str, limit: int = MAX_POST_CHARS) -> list[str]:
    """Chunks of at most `limit`, broken at a paragraph, then a line, then hard."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def _destination(turn):
    """(installation, channel, thread_ts) for a Slack-born session's turn, else None."""
    from .services import SLACK_THREAD_KEY, installation_for

    session = getattr(turn, "chat_session", None)
    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    if not meta.get(SLACK_THREAD_KEY):
        return None
    installation = installation_for(str(meta.get("slack_team") or ""))
    channel = str(meta.get("slack_channel") or "")
    if installation is None or not channel:
        return None
    return installation, channel, str(meta.get("slack_thread_ts") or "")


def _message_for(row) -> str:
    payload = row.payload or {}
    if row.kind == "assistant":
        return str(payload.get("text") or "").strip()
    if row.kind == "status" and payload.get("status") == "failed":
        note = str(payload.get("result_note") or "").strip()
        return "⚠️ This turn failed" + (f": {note}" if note else ".")
    return ""


def relay(turn, rows) -> int:
    """Post each reply row of `turn` into its Slack thread, once. Returns posts made."""
    if not getattr(turn, "chat_session_id", None):
        return 0
    dest = _destination(turn)
    if dest is None:
        return 0
    installation, channel, thread_ts = dest
    posted = 0
    for row in rows:
        text = _message_for(row)
        if not text:
            continue
        try:
            record = SlackRelayPost.objects.create(turn=turn, seq=row.seq, channel_id=channel)
        except IntegrityError:
            continue  # already relayed — a re-delivered signal, or a concurrent append
        try:
            ts = ""
            for chunk in split(to_mrkdwn(text)):
                ts = client.post_message(installation.bot_token, channel=channel, text=chunk,
                                         thread_ts=thread_ts)
            record.slack_ts = ts
            record.save(update_fields=["slack_ts"])
            posted += 1
        except Exception as e:  # noqa: BLE001 — a relay must never fail the runner's append
            logger.exception("could not relay a reply to Slack")
            record.error = str(e)[:200]
            record.save(update_fields=["error"])
            _log_failure(installation, turn, channel, str(e))
    return posted


def _log_failure(installation, turn, channel: str, error: str) -> None:
    from apps.events import services as events_services
    from apps.events.models import Event

    try:
        events_services.record([{
            "source": "slack",
            "kind": "slack.relay_failed",
            "level": Event.ERROR,
            # One row per channel, counting — a channel the bot was removed from
            # fails every reply until someone notices.
            "key": f"relay_failed:{channel}",
            "summary": f"could not post {turn.id}'s reply to Slack: {error}"[:500],
            "payload": {"turn": str(turn.id), "channel": channel},
        }], workspace=installation.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("could not record a Slack relay failure")
