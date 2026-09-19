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

import hashlib
import logging
import re

from django.db import IntegrityError, transaction

from . import client, menus
from .models import SlackMenuPost, SlackRelayPost

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
    return session_destination(getattr(turn, "chat_session", None))


def session_destination(session):
    """(installation, channel, thread_ts) for a Slack-born session, else None."""
    from .services import SLACK_THREAD_KEY, installation_for

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
            # A savepoint: a caught IntegrityError otherwise poisons whatever
            # transaction this runs inside (the same trap CLAUDE.md records for
            # SESSION_SAVE_EVERY_REQUEST).
            with transaction.atomic():
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


def _log_failure(installation, subject, channel: str, error: str) -> None:
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
            "summary": f"could not post {subject.id}'s reply to Slack: {error}"[:500],
            "payload": {"subject": str(subject.id), "channel": channel},
        }], workspace=installation.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("could not record a Slack relay failure")


def relay_menu(session_id, menu) -> bool:
    """Post a blocked agent's question into its Slack thread, once per question.

    An answerable question gets buttons (checkboxes/radios + Submit for pick-any
    or several questions); the text beside them stays the fallback, and typed
    replies still work. When the dialog clears, every question post that was
    not answered by a click is rewritten without its buttons — otherwise a
    thread keeps live buttons for a question that is over, and a tap would
    press a number at whatever the agent is showing now.

    Returns whether anything was posted.
    """
    from apps.canopy_sessions.models import Session

    from .services import session_url

    session = Session.objects.select_related("agent").filter(pk=session_id).first()
    dest = session_destination(session)
    if dest is None:
        return False
    installation, channel, thread_ts = dest
    if not menu:
        _close_question_posts(installation, session, ":white_check_mark: Answered.")
        SlackMenuPost.objects.filter(session=session).delete()
        return False
    agent_slug = session.agent.slug if session.agent_id else "the agent"
    link = session_url(session) if session.created_by_id else ""
    question = str(menu.get("question") or "")[:300]
    posts = [("q:" + menus.content_key(menu), menus.to_text(agent_slug, menu, link),
              menus.to_blocks(agent_slug, menu, session.id) if menus.answerable(menu) else None,
              question)]
    # A refused answer comes back INSIDE the menu (`answer_note`) on the next
    # report — say it where the person who answered is looking.
    if menu.get("answer_note"):
        note = str(menu["answer_note"])
        posts.append(("n:" + hashlib.sha256(note.encode()).hexdigest()[:60],
                      f":warning: That answer didn't land: {note}", None, ""))
    posted = False
    for key, text, blocks, asked in posts:
        try:
            with transaction.atomic():
                record = SlackMenuPost.objects.create(session=session, key=key[:64],
                                                      channel_id=channel, question=asked)
        except IntegrityError:
            continue
        try:
            record.slack_ts = client.post_message(installation.bot_token, channel=channel,
                                                  text=text, thread_ts=thread_ts, blocks=blocks)
            record.save(update_fields=["slack_ts"])
            posted = True
        except Exception as e:  # noqa: BLE001 — never break the runner's report over Slack
            logger.exception("could not post a question to Slack")
            _log_failure(installation, session, channel, str(e))
    return posted


def _close_question_posts(installation, session, outcome: str) -> None:
    """Rewrite this session's still-open question posts without their buttons."""
    open_posts = SlackMenuPost.objects.filter(session=session, key__startswith="q:", resolved=False) \
        .exclude(slack_ts="")
    for post in open_posts:
        try:
            client.update_message(installation.bot_token, channel=post.channel_id, ts=post.slack_ts,
                                  text=outcome, blocks=menus.resolved_blocks(post.question, outcome))
        except Exception:  # noqa: BLE001 — a stale button is a cosmetic failure, not a broken report
            logger.exception("could not close a Slack question post")
