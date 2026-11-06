"""Reply buttons on a post in a bound thread: a click is the reply, already typed.

An agent (or anyone sharing a session) attaches `actions` to a post — each a
label and the words it stands for. Pressing one sends those words to the bound
session as a message from the person who pressed it, through `services.deliver_choice`:
the end of the road a typed reply takes.

A post's buttons work independently. The first click on a button that reaches the
session settles that button: it becomes a line saying who chose what, and the others
stay pressable. An `exclusive` post takes one choice, and its first click settles the
whole post. The words chosen are shown, not just the label, so a button cannot say
one thing and send another unseen. A click that does not reach the session (no
standing in this conversation, a detached thread, a closed session, an agent blocked
on a question) leaves the button for someone who can, and tells the person it did
nothing.

The words live on `SlackChoicePost`, not in the button's value: what a click sends
is decided here, and the row is also what makes a double click deliver once.

Every click rewrites the same message, so the post is drawn from the row, never from
the click's copy of the message (out of date the moment another click lands), and
with the row locked across the call to Slack, so that the last rewrite Slack receives
is drawn from every claim made before it.
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
#: Starts the block id of a settled button's line; under `_BLOCK_PREFIX`, so that a
#: redraw replaces it along with the buttons.
_DONE_PREFIX = f"{_BLOCK_PREFIX}chosen_"


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


def _chose_line(slack_user_id: str, reply: str) -> str:
    return f":white_check_mark: <@{slack_user_id}> chose: *{_escaped(reply)}*"


def _escaped(words: str) -> str:
    """The agent's words, unable to ping a channel or forge a link in the settled line."""
    return words.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _button(action: dict, index: int, post_id) -> dict:
    button = {"type": "button", "action_id": f"{ACTION_PREFIX}{index}", "value": f"{post_id}:{index}",
              "text": {"type": "plain_text", "text": action["label"]}}
    if action.get("style"):
        button["style"] = action["style"]
    return button


def _choice_blocks(actions: list[dict], post_id, *, chosen: dict | None = None,
                   exclusive: bool = False) -> list[dict]:
    """The post's buttons in order, `ROW_SIZE` to a row, each settled one a line in
    its place. Once an exclusive post has a choice, the rest are gone."""
    chosen = chosen or {}
    blocks: list[dict] = []
    row: list[dict] = []
    rows = 0

    def end_row() -> None:
        nonlocal row, rows
        if row:
            blocks.append({"type": "actions", "block_id": f"{_BLOCK_PREFIX}{rows}", "elements": row})
            row, rows = [], rows + 1

    for i, action in enumerate(actions):
        settled = chosen.get(str(i))
        if settled:
            end_row()
            line = _chose_line(settled["by"], action["reply"])
            blocks.append({"type": "context", "block_id": f"{_DONE_PREFIX}{i}",
                           "elements": [{"type": "mrkdwn", "text": line}]})
        elif not (exclusive and chosen):
            row.append(_button(action, i, post_id))
            if len(row) == ROW_SIZE:
                end_row()
    end_row()
    return blocks


def blocks_for(text: str, actions: list[dict], post_id) -> list[dict]:
    """The post's words as sections, then its buttons."""
    sections = [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
                for chunk in relay.split(text, _SECTION_MAX)]
    return [*sections, *_choice_blocks(actions, post_id)]


def fallback(text: str) -> str:
    """`text` beside the blocks: what notifications and screen readers get."""
    return text if len(text) <= _SECTION_MAX else text[: _SECTION_MAX - 1].rstrip() + "…"


def remember(post_id, session, *, channel_id: str, ts: str, actions: list[dict], exclusive: bool = False) -> None:
    """Record a post just made. After the post, in the session's thread as it now stands."""
    SlackChoicePost.objects.create(
        id=post_id, session=session, channel_id=channel_id, slack_ts=ts, exclusive=exclusive,
        thread_key=str((session.metadata or {}).get(services.SLACK_THREAD_KEY) or ""),
        actions=[{"label": a["label"], "reply": a["reply"], "style": a["style"]} for a in actions])


def _redraw(installation, post_id, message: dict) -> None:
    """Rewrite the post as it was, with its buttons as they now stand.

    The row stays locked across the call: a click drawing behind this one waits, reads
    what this one could not have seen, and so reaches Slack last with all of it. Only
    the words come from `message`: they are not ours to change and no click changes them.
    """
    kept = [b for b in (message.get("blocks") or [])
            if not str(b.get("block_id") or "").startswith(_BLOCK_PREFIX)]
    try:
        with transaction.atomic():
            post = SlackChoicePost.objects.select_for_update().get(pk=post_id)
            drawn = _choice_blocks(post.actions, post.pk, chosen=post.chosen, exclusive=post.exclusive)
            words = [b["text"]["text"] for b in kept if (b.get("text") or {}).get("text")]
            lines = [_chose_line(c["by"], post.actions[int(i)]["reply"]) for i, c in sorted(
                post.chosen.items(), key=lambda item: int(item[0]))]
            client.update_message(installation.bot_token, channel=post.channel_id, ts=post.slack_ts,
                                  text=fallback("\n".join([*words, *lines])), blocks=[*kept, *drawn])
    except Exception:  # noqa: BLE001 — the reply landed, and a second click is already refused
        logger.exception("could not rewrite a Slack choice post")


def _nothing(status: str, why: str, session=None) -> services.Outcome:
    return services.Outcome(status, f"{why} Your click did nothing.", session=session)


def _settled_by(post: SlackChoicePost, index: int) -> tuple[int, dict] | None:
    """What stops this button being claimed: its own earlier press, or on an
    exclusive post, the post's one choice."""
    if str(index) in post.chosen:
        return index, post.chosen[str(index)]
    if post.exclusive and post.chosen:
        taken, settled = next(iter(post.chosen.items()))
        return int(taken), settled
    return None


def _already_chosen(post: SlackChoicePost, taken: tuple[int, dict]) -> services.Outcome:
    index, settled = taken
    reply = post.actions[index]["reply"]
    return _nothing(services.STALE, f"<@{settled['by']}> already chose: *{_escaped(reply)}*.", post.session)


def _claim(post: SlackChoicePost, index: int, slack_user_id: str) -> tuple[int, dict] | None:
    """Claim a button before it is delivered, so that of two clicks on it exactly one
    goes on. None when claimed, else what got there first."""
    with transaction.atomic():
        locked = SlackChoicePost.objects.select_for_update().get(pk=post.pk)
        taken = _settled_by(locked, index)
        if taken is None:
            locked.chosen[str(index)] = {"by": slack_user_id, "at": timezone.now().isoformat()}
            locked.save(update_fields=["chosen"])
    return taken


def _give_back(post: SlackChoicePost, index: int, slack_user_id: str) -> bool:
    """Undo a claim that did not reach the session. True when another click's claim
    still stands, whose rewrite may have drawn this one as chosen."""
    with transaction.atomic():
        locked = SlackChoicePost.objects.select_for_update().get(pk=post.pk)
        if (locked.chosen.get(str(index)) or {}).get("by") == slack_user_id:
            del locked.chosen[str(index)]
            locked.save(update_fields=["chosen"])
    return bool(locked.chosen)


def _find(installation, inbound: services.Inbound, action: dict) -> tuple[SlackChoicePost | None, int]:
    """The post and button a click names — only if the click came from that very
    message, in this Slack. A button's value is a claim, and is re-checked."""
    post_id, _, index = str(action.get("value") or "").partition(":")
    if not index.isdecimal():
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
    taken = _settled_by(post, index)
    if taken is not None:
        return _already_chosen(post, taken)
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

    taken = _claim(post, index, inbound.slack_user_id)
    if taken is not None:
        return _already_chosen(post, taken)

    reply = post.actions[index]["reply"]
    anchor = str(meta.get("slack_thread_ts") or "")
    is_dm = anchor == services.DM_ANCHOR
    typed = replace(inbound, text=reply, thread_ts="" if is_dm else anchor, is_dm=is_dm, follow=True,
                    delivery_key=f"{inbound.ts}:{index}")
    try:
        with transaction.atomic():
            outcome = services.deliver_choice(session, principal, reply, typed)
    except Exception:  # noqa: BLE001 — unlike other clicks, this person must hear it went nowhere
        logger.exception("could not deliver a Slack choice")
        outcome = _nothing("failed", "Something went wrong sending that to the session. It has been logged.",
                           session)
    if outcome.status not in _DONE:
        if _give_back(post, index, inbound.slack_user_id):
            _redraw(installation, post.pk, message)
        return outcome
    _redraw(installation, post.pk, message)
    return outcome
