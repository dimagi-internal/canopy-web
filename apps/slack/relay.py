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


#: When a SHARE bound this session to its thread (`apps/slack/share.py`), as epoch seconds.
BOUND_AT = "slack_bound_at"


def predates_bind(turn) -> bool:
    """Whether `turn` was asked before its session was bound to a Slack thread.

    A share from the chat page is itself a turn ("/canopy:share-to-slack …"),
    and it finishes AFTER the bind it performs — so without this its closing
    "Shared to <link>" and its done status line would open the new thread.
    Nothing asked before the bind is the thread's business.
    """
    session = getattr(turn, "chat_session", None)
    bound_at = ((getattr(session, "metadata", None) or {}) if session is not None else {}).get(BOUND_AT)
    created = getattr(turn, "created_at", None)
    return bool(bound_at and created and created.timestamp() < float(bound_at))


def _destination(turn):
    """(installation, channel, thread_ts) for a Slack-born session's turn, else None."""
    if predates_bind(turn):
        return None
    return session_destination(getattr(turn, "chat_session", None))


def persona(agent) -> dict | None:
    """Post as the agent — its name, and its avatar when Slack can fetch it."""
    if agent is None:
        return None
    out = {"username": (agent.name or agent.slug)[:80]}
    if str(agent.avatar_url or "").startswith("https://"):
        out["icon_url"] = agent.avatar_url
    return out


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
                                         thread_ts=thread_ts, persona=persona(turn.chat_session.agent))
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
    from . import status as status_mod

    status_mod.sync_session(session, dest)
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
                                                  text=text, thread_ts=thread_ts, blocks=blocks,
                                                  persona=persona(session.agent))
            record.save(update_fields=["slack_ts"])
            posted = True
        except Exception as e:  # noqa: BLE001 — never break the runner's report over Slack
            logger.exception("could not post a question to Slack")
            _log_failure(installation, session, channel, str(e))
    return posted


#: At most one "carried on elsewhere" notice per thread per window — a person
#: typing ten messages at the laptop is one fact for the thread, not ten.
ELSEWHERE_WINDOW_SECONDS = 30 * 60
ELSEWHERE_AT = "slack_elsewhere_at"


def notify_elsewhere(session, texts) -> bool:
    """Someone typed straight into this Slack-born conversation's session on its
    box (emdash), bypassing every canopy surface. That is not a Turn, so neither
    the reply relay nor a status line will ever see it, and the thread would
    silently fall behind. Say so, once per window, with the way to follow along.

    A message a TURN delivered also lands in the transcript as the human's
    words; those are recognised (a turn is executing, or the text is a recent
    turn's prompt) and ignored.
    """
    import time

    from apps.canopy_sessions.models import Session
    from apps.canopy_sessions.transcript_noise import is_system_noise
    from apps.harness.models import Turn

    from .services import session_url

    dest = session_destination(session)
    if dest is None:
        return False
    texts = [t.strip() for t in texts if t and t.strip() and not is_system_noise(t)]
    if not texts:
        return False
    turns = Turn.objects.filter(chat_session=session)
    if turns.filter(status__in=list(Turn.NON_TERMINAL - {Turn.QUEUED})).exists():
        return False
    recent = {" ".join((p or "").split()) for p in turns.order_by("-created_at")
              .values_list("prompt", flat=True)[:20]}
    if all(" ".join(t.split()) in recent for t in texts):
        return False
    now = time.time()
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(locked.metadata or {})
        if now - float(meta.get(ELSEWHERE_AT) or 0) < ELSEWHERE_WINDOW_SECONDS:
            return False
        meta[ELSEWHERE_AT] = now
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    installation, channel, thread_ts = dest
    binding = getattr(locked, "runner_binding", None)
    where = f" on *{binding.runner.name}*" if binding is not None and binding.runner_id else ""
    text = (f":eyes: This conversation is carrying on directly in the agent's session{where}, "
            "outside Slack — what's said there won't show up in this thread.")
    if locked.created_by_id:
        text += f" <{session_url(locked)}|Follow along in canopy>"
    try:
        client.post_message(installation.bot_token, channel=channel, text=text, thread_ts=thread_ts)
    except Exception as e:  # noqa: BLE001 — never break the runner's stream over Slack
        logger.exception("could not post a Slack elsewhere notice")
        _log_failure(installation, locked, channel, str(e))
        return False
    return True


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def on_transcript(session, rows) -> None:
    """A runner streamed transcript text for a session; keep its Slack thread in step.

    `rows` is [(index, kind, text)] in order. Human rows may be someone typing
    into the session directly (announced, not mirrored); agent rows may be text
    written after the turn closed (relayed, when it is still answering the thread).
    """
    if session_destination(session) is None:
        return
    users = [t for _i, k, t in rows if k == "user"]
    if users:
        notify_elsewhere(session, users)
    replies = [(i, t) for i, k, t in rows if k == "assistant"]
    if replies:
        relay_after_turn(session, replies)


def relay_after_turn(session, replies) -> int:
    """Post agent text written outside any turn, when it is still answering an
    ask the thread knows about. Returns posts made.

    The case this is for: emdash ends a turn the moment the agent yields to
    background work, so the ledger relay posts what was written up to then and
    nothing after — the actual result of waiting on CI, a merge or a deploy
    reached canopy-web and never the thread that asked for it.

    Three guards, each against a specific wrong post:

    * **a turn is executing** -> skip; the ledger relay owns that reply, and
      posting both would say everything twice.
    * **the latest human message is not an ask the thread saw** (a turn's
      prompt — Slack's own, or one announced by a status line) -> skip; the
      conversation moved into emdash, which `notify_elsewhere` says instead of
      mirroring a private exchange into a channel.
    * **the text was already bridged** into the last turn's ledger -> skip.
    """
    from apps.canopy_sessions.models import Message
    from apps.canopy_sessions.transcript_noise import is_system_noise
    from apps.harness.models import Turn, TurnEvent

    from .models import SlackTranscriptPost

    dest = session_destination(session)
    if dest is None:
        return 0
    turns = Turn.objects.filter(chat_session=session)
    if turns.filter(status__in=list(Turn.NON_TERMINAL - {Turn.QUEUED})).exists():
        return 0
    last = turns.order_by("-created_at").first()
    if last is None or predates_bind(last):
        return 0
    asks = {_norm(p) for p in turns.order_by("-created_at").values_list("prompt", flat=True)[:20]}
    latest_human = next(
        (m.plaintext for m in Message.objects.filter(session=session, role=Message.USER)
         .order_by("-turn_index")[:10] if not is_system_noise(m.plaintext or "")),
        None,
    )
    if latest_human is None or _norm(latest_human) not in asks:
        return 0
    bridged = {_norm(str((p or {}).get("text") or "")) for p in
               TurnEvent.objects.filter(turn=last, kind="assistant").values_list("payload", flat=True)}

    installation, channel, thread_ts = dest
    agent = session.agent if session.agent_id else None
    posted = 0
    for index, text in replies:
        text = (text or "").strip()
        if not text or _norm(text) in bridged:
            continue
        try:
            with transaction.atomic():
                record = SlackTranscriptPost.objects.create(session=session, index=index)
        except IntegrityError:
            continue
        try:
            ts = ""
            for chunk in split(to_mrkdwn(text)):
                ts = client.post_message(installation.bot_token, channel=channel, text=chunk,
                                         thread_ts=thread_ts, persona=persona(agent))
            record.slack_ts = ts
            record.save(update_fields=["slack_ts"])
            posted += 1
        except Exception as e:  # noqa: BLE001 — never break the runner's stream over Slack
            logger.exception("could not relay after-turn text to Slack")
            record.error = str(e)[:200]
            record.save(update_fields=["error"])
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
