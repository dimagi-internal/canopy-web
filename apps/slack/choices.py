"""Reply buttons on a post in a bound thread: a click is the reply, already typed.

An agent (or anyone sharing a session) attaches `actions` to a post — each a
label and the words it stands for. Pressing one sends those words to the bound
session as a message from the person who pressed it, through `services.deliver_choice`:
the end of the road a typed reply takes.

One post takes one choice. The first click that reaches the session settles it,
and the post is rewritten to say who chose what; the words chosen are shown, not
just the label, so a button cannot say one thing and send another unseen. A click
that does not reach the session (no standing in this conversation, a detached
thread, a closed session, an agent blocked on a question) leaves the buttons for
someone who can, and tells the person it did nothing.

The words live on `SlackChoicePost`, not in the button's value: what a click sends
is decided here, and the row is also what makes a double click deliver once.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from django.db import transaction
from django.utils import timezone

from . import client, relay, services
from .models import SlackChoicePost

logger = logging.getLogger(__name__)

#: `action_id` of a choice button is this plus its index in the post.
ACTION_PREFIX = "session_choice_"
_BLOCK_PREFIX = "session_choices_"
STYLES = ("primary", "danger")

#: A row of buttons, and how many a post may carry: Slack draws five or so across
#: and refuses a message over 50 blocks, and more choices than this is not a choice.
ROW_SIZE, MAX_ACTIONS = 5, 25
#: Slack's cap on a button's text; and on a section's, which holds the post's words
#: once it has blocks.
_LABEL_MAX, _SECTION_MAX = 75, 3000
_REPLY_MAX = 2000

_DONE = (services.SENT, services.ANSWERED)


def clean(actions) -> tuple[list[dict], str]:
    """The actions as stored and drawn, and a refusal sentence if they can't be."""
    cleaned: list[dict] = []
    if len(actions or []) > MAX_ACTIONS:
        return [], f"A post can carry at most {MAX_ACTIONS} actions."
    for i, action in enumerate(actions or [], 1):
        label = str(action.get("label") or "").strip()
        reply = str(action.get("reply") or "").strip()
        style = str(action.get("style") or "").strip()
        if not label or not reply:
            return [], f"Action {i} needs both a label and a reply."
        if len(reply) > _REPLY_MAX:
            return [], f"Action {i}'s reply is over {_REPLY_MAX} characters."
        if style and style not in STYLES:
            return [], f"Action {i}'s style must be one of {', '.join(STYLES)}, or left out."
        if len(label) > _LABEL_MAX:
            label = label[: _LABEL_MAX - 1].rstrip() + "…"
        cleaned.append({"label": label, "reply": reply, "style": style})
    return cleaned, ""


def new_post_id() -> uuid.UUID:
    """The id a post's buttons name, chosen before it is posted so they can carry it."""
    return uuid.uuid4()


def blocks_for(text: str, actions: list[dict], post_id) -> list[dict]:
    """The post's words as sections, then its buttons, `ROW_SIZE` to a row."""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
                          for chunk in relay.split(text, _SECTION_MAX)]
    buttons = []
    for i, action in enumerate(actions):
        button = {"type": "button", "action_id": f"{ACTION_PREFIX}{i}", "value": f"{post_id}:{i}",
                  "text": {"type": "plain_text", "text": action["label"]}}
        if action["style"]:
            button["style"] = action["style"]
        buttons.append(button)
    for row, start in enumerate(range(0, len(buttons), ROW_SIZE)):
        blocks.append({"type": "actions", "block_id": f"{_BLOCK_PREFIX}{row}",
                       "elements": buttons[start:start + ROW_SIZE]})
    return blocks


def fallback(text: str) -> str:
    """`text` beside the blocks: what notifications and screen readers get."""
    return text if len(text) <= _SECTION_MAX else text[: _SECTION_MAX - 1].rstrip() + "…"


def remember(post_id, session, *, channel_id: str, ts: str, actions: list[dict]) -> None:
    """Record a post just made. After the post, in the session's thread as it now stands."""
    SlackChoicePost.objects.create(
        id=post_id, session=session, channel_id=channel_id, slack_ts=ts,
        thread_key=str((session.metadata or {}).get(services.SLACK_THREAD_KEY) or ""),
        actions=[{"label": a["label"], "reply": a["reply"]} for a in actions])


def _escaped(words: str) -> str:
    """The agent's words, unable to ping a channel or forge a link in the settled line."""
    return words.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chose_line(slack_user_id: str, reply: str) -> str:
    return f":white_check_mark: <@{slack_user_id}> chose: *{_escaped(reply)}*"


def _settle_message(installation, post: SlackChoicePost, message: dict, line: str) -> None:
    """Rewrite the post as it was, minus its buttons, plus who chose what."""
    kept = [b for b in (message.get("blocks") or [])
            if not str(b.get("block_id") or "").startswith(_BLOCK_PREFIX)]
    blocks = [*kept, {"type": "context", "elements": [{"type": "mrkdwn", "text": line}]}]
    try:
        client.update_message(installation.bot_token, channel=post.channel_id, ts=post.slack_ts,
                              text=f"{message.get('text') or ''}\n{line}".strip(), blocks=blocks)
    except Exception:  # noqa: BLE001 — the reply landed, and a second click is already refused
        logger.exception("could not rewrite a Slack choice post")


def _nothing(status: str, why: str, session=None) -> services.Outcome:
    return services.Outcome(status, f"{why} Your click did nothing.", session=session)


def _already_chosen(post: SlackChoicePost) -> services.Outcome:
    reply = post.actions[post.chosen_index]["reply"]
    return _nothing(services.STALE, f"<@{post.chosen_by}> already chose: *{_escaped(reply)}*.", post.session)


def _find(installation, inbound: services.Inbound, action: dict) -> tuple[SlackChoicePost | None, int]:
    """The post and button a click names — only if the click came from that very
    message, in this Slack. A button's value is a claim, and is re-checked."""
    post_id, _, index = str(action.get("value") or "").partition(":")
    if not index.isdigit():
        return None, 0
    try:
        post = (SlackChoicePost.objects.select_related("session__agent")
                .filter(pk=post_id, channel_id=inbound.channel_id, slack_ts=inbound.ts,
                        session__in=services.tenant_sessions(installation))
                .first())
    except Exception:  # noqa: BLE001 — a malformed uuid is just "no such post"
        return None, 0
    if post is None or int(index) >= len(post.actions):
        return None, 0
    return post, int(index)


def click(installation, inbound: services.Inbound, action: dict, message: dict) -> services.Outcome:
    """A press of a choice button -> the reply delivered, or why it wasn't."""
    post, index = _find(installation, inbound, action)
    if post is None:
        return _nothing(services.STALE, "The session this button belonged to is gone.")
    session = post.session
    if post.chosen_at is not None:
        return _already_chosen(post)
    meta = session.metadata or {}
    if meta.get(services.SLACK_THREAD_KEY) != post.thread_key:
        return _nothing(services.STALE, "This thread is no longer connected to its session.", session)
    if session.status == session.ARCHIVED:
        return _nothing(services.SESSION_CLOSED, "This session is closed.", session)
    principal, refused = services.resolve_principal(installation, inbound.slack_user_id, session.workspace_id)
    if refused is not None:
        return refused
    refused = services.choice_refusal(session, principal)
    if refused is not None:
        return refused

    # Claimed before it is delivered, so that of two clicks exactly one goes on.
    chosen = SlackChoicePost.objects.filter(pk=post.pk, chosen_at__isnull=True).update(
        chosen_at=timezone.now(), chosen_index=index, chosen_by=inbound.slack_user_id)
    if not chosen:
        post.refresh_from_db()
        return _already_chosen(post)

    reply = post.actions[index]["reply"]
    anchor = str(meta.get("slack_thread_ts") or "")
    is_dm = anchor == services.DM_ANCHOR
    typed = replace(inbound, text=reply, thread_ts="" if is_dm else anchor, is_dm=is_dm, follow=True)
    try:
        with transaction.atomic():
            outcome = services.deliver_choice(session, principal, reply, typed)
    except Exception:  # noqa: BLE001 — unlike other clicks, this person must hear it went nowhere
        logger.exception("could not deliver a Slack choice")
        outcome = _nothing("failed", "Something went wrong sending that to the session. It has been logged.",
                           session)
    if outcome.status not in _DONE:
        SlackChoicePost.objects.filter(pk=post.pk).update(chosen_at=None, chosen_index=None, chosen_by="")
        return outcome
    _settle_message(installation, post, message, _chose_line(inbound.slack_user_id, reply))
    return outcome
