"""A session shares itself TO Slack — the front door, run backwards.

Everywhere else in this app a thread is born in Slack and adopts a session.
Here the session exists first (you are working in it) and posts a summary into
a channel, in one of two modes:

* **broadcast** — one top-level post. Nothing about the thread is recorded, so
  replies stay between people and never reach the session.
* **bind** — the same post, and then the session takes that thread: it gets the
  exact metadata a Slack-born session carries (`services.SLACK_THREAD_KEY` and
  friends), so relay, thread replies, menus and buttons all treat it as one.
  Replying in the thread steers the session — which is why bind is asked for,
  never assumed.

The summary is written by the SESSION (the canopy plugin's
`/canopy:share-to-slack` skill, reached from the terminal or sent by the chat
page) and arrives here as text. This module decides only whether the share is
allowed and where it lands. Design:
`docs/superpowers/specs/2026-09-21-share-session-to-slack-design.md`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from apps.canopy_sessions.access import visible_session_q
from apps.canopy_sessions.models import Session
from apps.workspaces import services as wsvc

from . import client, relay, services
from .models import SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

BROADCAST, BIND = "broadcast", "bind"
MODES = (BROADCAST, BIND)

SHARED = "shared"
NOT_INSTALLED, NOT_LINKED, NO_SESSION, ALREADY_BOUND, AGENT_NOT_ON_SLACK, NOT_IN_CHANNEL, \
    AMBIGUOUS_WORKSPACE, POST_FAILED, BAD_REQUEST = (
        "not_installed", "not_linked", "no_session", "already_bound", "agent_not_on_slack",
        "not_in_channel", "ambiguous_workspace", "post_failed", "bad_request")

#: Slack's answers that mean "the bot cannot post there", as opposed to a fault.
_CANNOT_POST_THERE = frozenset({"not_in_channel", "channel_not_found", "is_archived"})


class Ambiguous(Exception):
    """More than one session matches — refused rather than guessed."""


@dataclass
class ShareResult:
    status: str
    message: str
    session: Session | None = None
    channel_id: str = ""
    ts: str = ""
    permalink: str = ""

    @property
    def ok(self) -> bool:
        return self.status == SHARED


def resolve_session(user, *, session_id: str = "", claude_session_id: str = "",
                    emdash_task: str = "", emdash_project: str = "") -> Session | None:
    """The session the caller means, among the ones they can open — or None.

    Called from inside a Claude Code process, which knows its own identity
    (its Claude session id, its emdash task) but not canopy's. Every candidate
    is filtered through the caller's workspaces and `visible_session_q` first,
    so no identifier can reach a session the caller could not open anyway.
    Raises `Ambiguous` when the identifiers match more than one.
    """
    slugs = wsvc.user_workspace_slugs(user)
    visible = Session.objects.filter(visible_session_q(user), workspace_id__in=slugs) \
        .select_related("agent").distinct()
    if session_id:
        try:
            return visible.filter(pk=session_id).first()
        except Exception:  # noqa: BLE001 — a malformed uuid is just "no such session"
            return None
    if claude_session_id:
        found = list(visible.filter(runner_binding__transcript_id=claude_session_id)[:2])
    elif emdash_task:
        qs = visible.filter(runner_binding__session_key=emdash_task)
        if emdash_project:
            qs = qs.filter(runner_binding__emdash_project=emdash_project)
        found = list(qs[:2])
    else:
        return None
    if len(found) > 1:
        raise Ambiguous("more than one session matches")
    return found[0] if found else None


def _installation(user, session: Session | None, workspace: str) -> tuple[SlackInstallation | None, str]:
    """The Slack install to post through, and a refusal sentence if there is none."""
    if session is not None:
        slugs = {session.workspace_id}
    elif workspace:
        slugs = {workspace} & wsvc.user_workspace_slugs(user)
    else:
        slugs = wsvc.user_workspace_slugs(user)
    installs = list(SlackInstallation.objects.select_related("workspace").filter(workspace_id__in=slugs)[:2])
    if not installs:
        return None, NOT_INSTALLED
    if len(installs) > 1:
        return None, AMBIGUOUS_WORKSPACE
    return installs[0], ""


def _slack_user(installation: SlackInstallation, user) -> str:
    """The caller's Slack user id in this install — linking by email if Slack knows it.

    The same rule the front door and the manual link page apply: the Slack
    account's email equals the canopy one. Anything else stays unlinked.
    """
    link = SlackUserLink.objects.filter(installation=installation, user=user).first()
    if link is not None:
        return link.slack_user_id
    if not user.email:
        return ""
    slack_id = client.lookup_user_id(installation.bot_token, user.email)
    if not slack_id:
        return ""
    link, _ = SlackUserLink.objects.get_or_create(
        installation=installation, slack_user_id=slack_id, defaults={"user": user})
    return slack_id if link.user_id == user.pk else ""


def _text(slack_user: str, summary: str, session: Session | None, mode: str) -> str:
    head = f"<@{slack_user}> shared what they're working on"
    if session is not None:
        target = f"`{session.agent.slug}`" if session.agent_id else (f"`{session.project}`" if session.project else "")
        if target:
            head += f" with {target}"
        head += f" · <{services.session_url(session)}|open in canopy>"
    body = relay.to_mrkdwn(summary.strip())
    text = f"{head}\n\n{body}"
    if mode == BIND:
        text += "\n\n_Reply in this thread to talk to this session._"
    return text


def _record(installation, user, status: str, summary: str, payload: dict, *, ok: bool) -> None:
    from apps.events import services as events_services
    from apps.events.models import Event

    try:
        events_services.record([{
            "source": "slack",
            "kind": "slack.shared" if ok else "slack.share_refused",
            "level": Event.INFO if ok else Event.WARN,
            "key": f"share:{status}:{user.pk}:{payload.get('channel', '')}:{payload.get('ts', '')}",
            "summary": summary[:500],
            "payload": payload,
        }], workspace=installation.workspace)
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the share
        logger.exception("could not record a Slack share event")


def share_session(user, *, channel: str, summary: str, mode: str = BROADCAST,
                  session: Session | None = None, workspace: str = "") -> ShareResult:
    """Post `summary` into `channel`; in BIND mode, give the session that thread.

    `session` is re-checked against what the caller can see — a caller that got
    it some other way than `resolve_session` gains nothing. None means "no
    session" and is fine for a broadcast.
    """
    if session is not None and resolve_session(user, session_id=str(session.pk)) is None:
        session = None
        if mode == BROADCAST:
            return ShareResult(NO_SESSION, "You can't share a session you can't open.")
    channel = (channel or "").strip()
    if mode not in MODES:
        return ShareResult(BAD_REQUEST, f"Mode must be one of {', '.join(MODES)}.")
    if not channel or not (summary or "").strip():
        return ShareResult(BAD_REQUEST, "A channel and a summary are both required.")
    if mode == BIND and session is None:
        return ShareResult(NO_SESSION, (
            "Couldn't find this session in canopy, so there's nothing for the thread's replies "
            "to reach. Share it as a broadcast instead, or check the session is one canopy knows."))

    installation, refused = _installation(user, session, workspace)
    if installation is None:
        if refused == AMBIGUOUS_WORKSPACE:
            return ShareResult(refused, "You're in several workspaces with Slack connected — say which one.")
        return ShareResult(NOT_INSTALLED, "Slack isn't connected to this workspace in canopy.")

    def refuse(status: str, message: str) -> ShareResult:
        _record(installation, user, status, message, {"channel": channel, "mode": mode,
                "session": str(session.pk) if session else ""}, ok=False)
        return ShareResult(status, message, session=session)

    if mode == BIND:
        meta = session.metadata or {}
        if meta.get(services.SLACK_THREAD_KEY):
            return refuse(ALREADY_BOUND, (
                f"This session is already bound to a thread in <#{meta.get('slack_channel', '')}>. "
                "Post there, or share a broadcast."))
        if session.agent_id and not session.agent.slack_enabled:
            return refuse(AGENT_NOT_ON_SLACK, (
                f"`{session.agent.slug}` isn't turned on for Slack, so a thread can't talk to it. "
                "Its owner can turn it on, or share a broadcast instead."))

    slack_user = _slack_user(installation, user)
    if not slack_user:
        return refuse(NOT_LINKED, (
            "Your canopy account isn't linked to a Slack user here. Mention @canopy once in Slack "
            "to link it, then share again."))

    agent = session.agent if session is not None and session.agent_id else None
    try:
        body = client.post_message_body(installation.bot_token, channel=channel,
                                        text=_text(slack_user, summary, session, mode),
                                        persona=relay.persona(agent))
    except client.SlackApiError as e:
        if e.error in _CANNOT_POST_THERE:
            return refuse(NOT_IN_CHANNEL, (
                f"canopy can't post in {channel} ({e.error}). Invite the canopy app to the channel "
                f"(`/invite @canopy` in {channel}) and share again."))
        logger.exception("slack share post failed")
        return refuse(POST_FAILED, f"Slack refused the post: {e.error}.")
    channel_id, ts = str(body.get("channel") or channel), str(body.get("ts") or "")

    if mode == BIND:
        with transaction.atomic():
            locked = Session.objects.select_for_update().get(pk=session.pk)
            meta = dict(locked.metadata or {})
            meta.update({
                services.SLACK_THREAD_KEY: services.thread_key(installation.team_id, channel_id, ts),
                "slack_team": installation.team_id,
                "slack_channel": channel_id,
                "slack_thread_ts": ts,
                "slack_shared_by": user.pk,
                # The share was typed into the session itself, and that line
                # reaches the transcript AFTER this bind — which would otherwise
                # open the new thread with "this is carrying on outside Slack".
                relay.ELSEWHERE_AT: time.time(),
            })
            locked.metadata = meta
            locked.save(update_fields=["metadata", "updated_at"])
        session.metadata = meta

    link = client.permalink(installation.bot_token, channel=channel_id, ts=ts)
    where = link or f"<#{channel_id}>"
    message = (f"Shared to {where}. Replies in that thread now reach this session." if mode == BIND
               else f"Shared to {where}.")
    _record(installation, user, SHARED, message, {"channel": channel_id, "ts": ts, "mode": mode,
            "session": str(session.pk) if session else ""}, ok=True)
    return ShareResult(SHARED, message, session=session, channel_id=channel_id, ts=ts, permalink=link)


def bound_repo_session(installation: SlackInstallation, key: str) -> Session | None:
    """An agent-less session bound to this thread by a share, if there is one.

    The front door finds a thread's session through its agent; a repo session
    has none, so it needs this second lookup.
    """
    return (Session.objects.filter(Q(agent__isnull=True), workspace=installation.workspace,
                                   **{f"metadata__{services.SLACK_THREAD_KEY}": key})
            .order_by("created_at").first())
