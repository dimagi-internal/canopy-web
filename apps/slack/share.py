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

from apps.canopy_sessions.access import readable_sessions
from apps.canopy_sessions.models import Session
from apps.harness import sensitivity
from apps.workspaces import services as wsvc

from . import client, location, relay, services
from .models import SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

BROADCAST, BIND = "broadcast", "bind"
MODES = (BROADCAST, BIND)

SHARED = "shared"
#: `start_new_thread`: the conversation now lives in a new thread of the same channel.
MOVED = "moved"
#: The session holds no Slack thread to update or move.
NOT_BOUND_THREAD = "not_bound"
#: A direct message is one running conversation; it has no threads to move between.
IS_DM = "is_dm"
#: A bound session shared again: the summary went into its thread as a reply.
UPDATED = "updated"
NOT_INSTALLED, NOT_LINKED, NO_SESSION, AGENT_NOT_ON_SLACK, NOT_IN_CHANNEL, \
    AMBIGUOUS_WORKSPACE, POST_FAILED, BAD_REQUEST = (
        "not_installed", "not_linked", "no_session", "agent_not_on_slack",
        "not_in_channel", "ambiguous_workspace", "post_failed", "bad_request")

#: The conversation holds a host's sensitive data and never goes to Slack
#: (`apps.harness.sensitivity`; spec 2026-10-22-sensitive-data-agents §4).
SENSITIVE = "sensitive"

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
    #: The channel's name when canopy knows it (`location.py`); "" otherwise.
    channel_name: str = ""
    #: `start_new_thread` only: the thread the conversation moved out of.
    previous: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status in (SHARED, UPDATED, MOVED)


def resolve_session(user, *, session_id: str = "", claude_session_id: str = "",
                    emdash_task: str = "", emdash_project: str = "", visible=None) -> Session | None:
    """The session the caller means, among the ones they can open — or None.

    Called from inside a Claude Code process, which knows its own identity
    (its Claude session id, its emdash task) but not canopy's. Every candidate
    is filtered through the session read rule (`access.readable_sessions`) first,
    so no identifier can reach a session the caller could not open anyway.
    Raises `Ambiguous` when the identifiers match more than one. `visible`
    narrows the candidates further (a REST caller's pinned workspace, a
    connected site's own agents); it is never wider than the read rule.
    """
    visible = (visible if visible is not None else readable_sessions(user)).select_related("agent")
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


def resolve_turn_session(user, turn_id: str, *, visible=None) -> tuple[Session | None, bool]:
    """The conversation of turn `turn_id`, and whether it was reached as the
    caller's OWN live turn (`access.own_turn_session`) rather than by reading it.

    An agent's own login reaches its turn's session only that way; anyone else
    (the person who started it, a caller token running as them) reaches it by
    the ordinary read rule, exactly as if they had named the session."""
    from apps.canopy_sessions import access

    own = access.own_turn_session(user, turn_id)
    if own is not None:
        return own, True
    from apps.harness.models import Turn

    try:
        sid = Turn.objects.filter(pk=turn_id).values_list("chat_session_id", flat=True).first()
    except Exception:  # noqa: BLE001 — a malformed id is just "no such turn"
        return None, False
    if not sid:
        return None, False
    return resolve_session(user, session_id=str(sid), visible=visible), False


def conversation_channel(session: Session) -> str:
    """The channel this conversation lives (or last lived) in — the only one an
    agent may re-bind it to on its own say-so."""
    meta = session.metadata or {}
    channel = str(meta.get("slack_channel") or "")
    if channel:
        return channel
    history = meta.get(DETACHED_KEY) or []
    return str((history[-1] or {}).get("channel") or "") if history else ""


def _installation(user, session: Session | None, workspace: str) -> tuple[SlackInstallation | None, str]:
    """The Slack install to post through, and a refusal sentence if there is none."""
    if session is not None:
        slugs = {session.workspace_id}
    elif workspace:
        slugs = {workspace} & wsvc.user_workspace_slugs(user)
    else:
        slugs = wsvc.user_workspace_slugs(user)
    # A session posts only through ITS tenant's Slack: a bound thread's replies
    # route back by tenant, so a Slack that does not serve the session's
    # workspace could never deliver them.
    installs = list(SlackInstallation.objects.filter(links__workspace_id__in=slugs).distinct()[:2])
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


def _agent_identity(user):
    """The agent this canopy user IS (`Agent.user`), or None for a person."""
    from apps.agents.models import Agent

    return Agent.objects.filter(user=user).first()


def _asker(session: Session | None, caller):
    """The person whose live turn in this session the agent is answering, or None.

    Only a turn still in flight counts: the share happens INSIDE the turn that
    asked for it, and a finished turn's person did not ask for this post. A
    turn the agent started itself (a schedule) has the agent as its initiator,
    which is not a person to credit.
    """
    if session is None:
        return None
    from apps.harness.models import Turn

    turn = (Turn.objects.filter(chat_session=session, initiator_user__isnull=False,
                                status__in=(Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN))
            .exclude(initiator_user=caller).select_related("initiator_user")
            .order_by("-created_at").first())
    return turn.initiator_user if turn is not None else None


def _byline(installation: SlackInstallation, user, session: Session | None = None) -> tuple[str, object]:
    """Who the post names as the sharer, and the agent the caller is (or None).

    A person is @-mentioned, so must be linked. An agent calling as itself has
    no Slack account to link (`hal@dimagi-ai.com` is not a Slack user), so it is
    named in bold and the post goes out under its persona. When a person's turn
    is what the agent is answering, they are credited beside it — never AS the
    sharer, because they did not write the post. A scheduled watch has no such
    person and posts under the agent's name alone. "" means neither: a person
    Slack does not know, which is refused.
    """
    slack_user = _slack_user(installation, user)
    if slack_user:
        return f"<@{slack_user}>", None
    agent = _agent_identity(user)
    if agent is None:
        return "", None
    byline = f"*{agent.name or agent.slug}*"
    asker = _asker(session, user)
    asker_slack = _slack_user(installation, asker) if asker is not None else ""
    if asker_slack:
        byline += f" (for <@{asker_slack}>)"
    return byline, agent


def _update_text(byline: str, summary: str) -> str:
    return f"*Update* from {byline}:\n\n{relay.to_mrkdwn(summary.strip())}"


def _text(byline: str, summary: str, session: Session | None, mode: str) -> str:
    head = f"{byline} shared what they're working on"
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


def _record(installation, user, status: str, summary: str, payload: dict, *, ok: bool,
            kind: str = "") -> None:
    from apps.events import services as events_services
    from apps.events.models import Event
    from apps.workspaces.models import Workspace

    session = Session.objects.filter(pk=payload.get("session") or None).first() if payload.get("session") else None
    workspace_id = session.workspace_id if session is not None else installation.home_workspace_id
    if workspace_id is None:
        return

    try:
        events_services.record([{
            "source": "slack",
            "kind": kind or ("slack.shared" if ok else "slack.share_refused"),
            "level": Event.INFO if ok else Event.WARN,
            "key": f"share:{status}:{user.pk}:{payload.get('channel', '')}:{payload.get('ts', '')}",
            "summary": summary[:500],
            "payload": payload,
        }], workspace=Workspace.objects.get(pk=workspace_id))
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the share
        logger.exception("could not record a Slack share event")


def share_session(user, *, channel: str, summary: str, mode: str = BROADCAST,
                  session: Session | None = None, workspace: str = "",
                  own_turn: bool = False) -> ShareResult:
    """Post `summary` into `channel`; in BIND mode, give the session that thread.

    `session` is re-checked against what the caller can see — a caller that got
    it some other way than `resolve_session` gains nothing. None means "no
    session" and is fine for a broadcast.

    A session that is ALREADY bound posts an update into its own thread instead,
    whatever the mode and channel: once bound, what you do at the terminal never
    reaches the thread on its own (the thread hears only what it asked), so
    sharing again is the way to keep the people watching it current. A session
    holds one thread, so there is no second post to make elsewhere.

    `own_turn` says `session` was reached as the caller's own live turn
    (`resolve_turn_session`) — an agent acting on the conversation it is having,
    which it cannot read. Such a caller may update the thread, and may BIND only
    back into the conversation's own channel: moving a person's conversation to
    a channel they never chose is theirs to ask for, not the agent's to do.
    """
    if session is not None and not own_turn and resolve_session(user, session_id=str(session.pk)) is None:
        session = None
        if mode == BROADCAST:
            return ShareResult(NO_SESSION, "You can't share a session you can't open.")
    if session is not None and sensitivity.is_sensitive(session):
        return ShareResult(SENSITIVE, sensitivity.REFUSAL)
    channel = (channel or "").strip()
    if mode not in MODES:
        return ShareResult(BAD_REQUEST, f"Mode must be one of {', '.join(MODES)}.")
    if not (summary or "").strip():
        return ShareResult(BAD_REQUEST, "A summary is required.")
    if session is not None and relay.session_destination(session) is not None:
        return _post_update(user, session, summary)
    if own_turn and session is not None:
        if mode != BIND:
            return ShareResult(BAD_REQUEST, (
                "An agent shares its own conversation only into that conversation's channel: "
                "post an update to its thread, bind it again, or start a new thread there."))
        home = conversation_channel(session)
        if not home:
            return ShareResult(BAD_REQUEST, "This conversation has never been in a Slack channel to go back to.")
        cached = ((session.metadata or {}).get(location.LOCATION_KEY) or {}).get("channel_name") or ""
        if channel and channel.lstrip("#") not in {home, cached} - {""}:
            return ShareResult(BAD_REQUEST, (
                f"An agent can only re-bind its conversation in the channel it was in (<#{home}>), "
                f"not {channel}. Ask the person to share it elsewhere."))
        channel = home
    if not channel:
        return ShareResult(BAD_REQUEST, "Name a channel to share to.")
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
        if session.agent_id and not session.agent.slack_enabled:
            return refuse(AGENT_NOT_ON_SLACK, (
                f"`{session.agent.slug}` isn't turned on for Slack, so a thread can't talk to it. "
                "Its owner can turn it on, or share a broadcast instead."))

    byline, caller_agent = _byline(installation, user, session)
    if not byline:
        return refuse(NOT_LINKED, (
            "Your canopy account isn't linked to a Slack user here. Mention @canopy once in Slack "
            "to link it, then share again."))

    agent = session.agent if session is not None and session.agent_id else caller_agent
    try:
        body = client.post_message_body(installation.bot_token, channel=channel,
                                        text=_text(byline, summary, session, mode),
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
        now = time.time()
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
                relay.ELSEWHERE_AT: now,
                relay.BOUND_AT: now,
            })
            locked.metadata = meta
            locked.save(update_fields=["metadata", "updated_at"])
        session.metadata = meta

    link = client.permalink(installation.bot_token, channel=channel_id, ts=ts)
    if mode == BIND:
        _learn_location(session, installation, permalink=link)
    where = link or f"<#{channel_id}>"
    message = (f"Shared to {where}. Replies in that thread now reach this session." if mode == BIND
               else f"Shared to {where}.")
    _record(installation, user, SHARED, message, {"channel": channel_id, "ts": ts, "mode": mode,
            "session": str(session.pk) if session else ""}, ok=True)
    return ShareResult(SHARED, message, session=session, channel_id=channel_id, ts=ts, permalink=link)


def _post_update(user, session: Session, summary: str) -> ShareResult:
    """A new summary, as a reply in the thread this session is already bound to."""
    installation, channel_id, thread_ts = relay.session_destination(session)

    def refuse(status: str, message: str) -> ShareResult:
        _record(installation, user, status, message, {"channel": channel_id, "mode": "update",
                "session": str(session.pk)}, ok=False)
        return ShareResult(status, message, session=session)

    byline, caller_agent = _byline(installation, user, session)
    if not byline:
        return refuse(NOT_LINKED, (
            "Your canopy account isn't linked to a Slack user here. Mention @canopy once in Slack "
            "to link it, then share again."))
    agent = session.agent if session.agent_id else caller_agent
    try:
        ts = ""
        for chunk in relay.split(_update_text(byline, summary)):
            ts = client.post_message(installation.bot_token, channel=channel_id, text=chunk,
                                     thread_ts=thread_ts, persona=relay.persona(agent))
    except client.SlackApiError as e:
        if e.error in _CANNOT_POST_THERE:
            return refuse(NOT_IN_CHANNEL, (
                f"canopy can't post in <#{channel_id}> any more ({e.error}). Invite the canopy app back "
                "to the channel and share again."))
        logger.exception("slack share update failed")
        return refuse(POST_FAILED, f"Slack refused the post: {e.error}.")
    link = client.permalink(installation.bot_token, channel=channel_id, ts=ts)
    message = f"Posted an update to this session's thread: {link or f'<#{channel_id}>'}."
    _record(installation, user, UPDATED, message, {"channel": channel_id, "ts": ts, "mode": "update",
            "session": str(session.pk)}, ok=True)
    return ShareResult(UPDATED, message, session=session, channel_id=channel_id, ts=ts, permalink=link)


# ---- detach --------------------------------------------------------------------

DETACHED = "detached"
#: Nothing to detach: the session holds no Slack thread (never bound, or already detached).
NOT_BOUND = "not_bound"

#: Where a detached binding is kept: a list, oldest first, of the threads this
#: session was connected to and then disconnected from, with who did it and when.
#: History only — nothing routes on it (`retention.policy` reads it to keep the
#: session classed as a Slack conversation).
DETACHED_KEY = "slack_detached"
_DETACHED_HISTORY = 20

#: The binding itself, plus the per-thread bookkeeping that would otherwise leak
#: into the NEXT thread after a re-share: what `share_session` (bind) and the
#: front door write, and what relay keeps about the thread it posts into.
_BINDING_KEYS = (
    services.SLACK_THREAD_KEY, "slack_team", "slack_channel", "slack_thread_ts", "slack_shared_by",
    relay.BOUND_AT, relay.ELSEWHERE_AT, relay.CLOSED_NOTICE, "slack_closed_told",
    location.LOCATION_KEY,
)

DETACH_NOTE = (":heavy_minus_sign: This thread is no longer connected to the session — "
               "replies here won't reach it.")


@dataclass
class DetachResult:
    status: str
    message: str
    session: Session
    channel_id: str = ""
    thread_ts: str = ""
    notice_posted: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (DETACHED, NOT_BOUND)


def binding_of(session: Session) -> dict | None:
    """The Slack thread this session is connected to, for showing it — or None.

    `url` opens the thread in Slack's web client (which hands off to the app);
    it needs no API call, so it is cheap enough for every session row.
    """
    meta = session.metadata or {}
    key = meta.get(services.SLACK_THREAD_KEY)
    if not key:
        return None
    team = str(meta.get("slack_team") or "")
    channel = str(meta.get("slack_channel") or "")
    thread_ts = str(meta.get("slack_thread_ts") or "")
    url = ""
    if team and channel:
        url = f"https://app.slack.com/client/{team}/{channel}"
        if thread_ts and thread_ts != services.DM_ANCHOR:
            url += f"/thread/{channel}-{thread_ts}"
    return {"channel": channel, "thread_ts": thread_ts, "url": url,
            "shared": bool(meta.get("slack_shared_by"))}


def detach_session(user, session: Session) -> DetachResult:
    """Disconnect `session` from its Slack thread. Idempotent.

    Soft: the binding keys are removed — which is what every path keys on, out
    (`relay.session_destination`: replies, status lines, menus, transcript
    relay) and in (`services.has_thread_session`, `bound_repo_session`, the
    agent lookup, button clicks matched on `slack_channel`) — and moved, with
    who and when, onto `DETACHED_KEY`. Nothing posted in Slack is deleted. A
    plain reply in the thread afterwards is dropped unread like any thread
    canopy is not in; an explicit `@canopy` there is a NEW ask, as anywhere.

    Re-attaching is an ordinary bind (`share_session(mode=BIND)`): it posts a
    fresh thread rather than reviving this one, which has just been told it is
    disconnected. Permission is the CALLER's (the route checks write access);
    this does only the work.
    """
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(locked.metadata or {})
        key = meta.get(services.SLACK_THREAD_KEY)
        if not key:
            session.metadata = meta
            return DetachResult(NOT_BOUND, "This session isn't connected to a Slack thread.", session=session)
        dest = relay.session_destination(locked)
        record = {
            "thread": key,
            "team": str(meta.get("slack_team") or ""),
            "channel": str(meta.get("slack_channel") or ""),
            "thread_ts": str(meta.get("slack_thread_ts") or ""),
            "shared_by": meta.get("slack_shared_by"),
            "bound_at": meta.get(relay.BOUND_AT),
            "detached_at": time.time(),
            "detached_by": user.pk,
        }
        for k in _BINDING_KEYS:
            meta.pop(k, None)
        meta[DETACHED_KEY] = (list(meta.get(DETACHED_KEY) or []) + [record])[-_DETACHED_HISTORY:]
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = meta

    from .models import SlackMenuPost

    # Question posts are keyed per (session, question): left behind, an open
    # question would never be posted to a thread this session binds to next.
    # Their buttons already answer "no longer open" — a click is matched on the
    # session's `slack_channel`, which is gone.
    SlackMenuPost.objects.filter(session=session).delete()

    posted = False
    if dest is not None:
        installation, channel_id, thread_ts = dest
        try:
            client.post_message(installation.bot_token, channel=channel_id, thread_ts=thread_ts,
                                text=DETACH_NOTE,
                                persona=relay.persona(session.agent if session.agent_id else None))
            posted = True
        except Exception:  # noqa: BLE001 — the detach has happened; the note is a courtesy
            logger.exception("could not post the Slack detach note")
        try:
            # Clear Slack's own "working…" indicator: nothing will update it now.
            client.set_session_status(installation.bot_token, channel=channel_id,
                                      thread_ts=thread_ts, status=client.ACTIVE)
        except Exception:  # noqa: BLE001
            logger.exception("could not clear the Slack agent-session status")
        message = f"Disconnected from <#{channel_id}>. Replies in that thread no longer reach this session."
        _record(installation, user, DETACHED, message,
                {"channel": channel_id, "ts": thread_ts, "mode": "detach", "session": str(session.pk)},
                ok=True, kind="slack.detached")
    else:
        message = "Disconnected from Slack. Replies in that thread no longer reach this session."
    return DetachResult(DETACHED, message, session=session, channel_id=record["channel"],
                        thread_ts=record["thread_ts"], notice_posted=posted)


def bound_repo_session(installation: SlackInstallation, key: str) -> Session | None:
    """An agent-less session bound to this thread by a share, if there is one.

    The front door finds a thread's session through its agent; a repo session
    has none, so it needs this second lookup.
    """
    return (services.tenant_sessions(installation).filter(Q(agent__isnull=True),
                                                          **{f"metadata__{services.SLACK_THREAD_KEY}": key})
            .order_by("created_at").first())


def _learn_location(session: Session | None, installation, *, permalink: str = "") -> None:
    """Cache where a just-bound thread is, for the caller envelope. Best-effort."""
    if session is None:
        return
    try:
        location.remember(session, permalink=permalink)
        location.fill(session, installation)
    except Exception:  # noqa: BLE001 — the bind has happened; this only names it
        logger.exception("could not record a bound Slack thread's location")


# ---- move to a new thread ------------------------------------------------------

MOVED_NOTE = ":arrow_right: This conversation continues in a new thread: {where}. Replies here won't reach it."


def start_new_thread(user, session: Session, summary: str) -> ShareResult:
    """Move this conversation to a NEW thread in the channel it is in.

    One step where it would otherwise take two (detach, then bind) and a guess
    at the channel: posts `summary` top-level in the conversation's own channel,
    binds the session to that post's thread exactly as a share-with-bind does,
    and tells the old thread where it went. The old binding is kept on
    `DETACHED_KEY` as history (with `moved_to`), like a detach.

    The turn that asked for the move is done with the old thread: its status
    line there is closed off with a pointer, and its own reply is not relayed
    into the new thread (`relay.BOUND_AT`), which opens with `summary` alone —
    the "clean thread" the move was asked for.

    Permission is the caller's (the route: write access, or the agent's own live
    turn); this does only the work.
    """
    if sensitivity.is_sensitive(session):
        return ShareResult(SENSITIVE, sensitivity.REFUSAL, session=session)
    if not (summary or "").strip():
        return ShareResult(BAD_REQUEST, "A summary is required: it is what the new thread opens with.",
                           session=session)
    dest = relay.session_destination(session)
    if dest is None:
        return ShareResult(NOT_BOUND_THREAD, "This conversation isn't in a Slack thread, so there's "
                           "nothing to move. Share it with `bind` to start one.", session=session)
    installation, channel_id, old_ts = dest
    before = location.location_of(session) or {}
    if not old_ts or before.get("is_dm"):
        return ShareResult(IS_DM, "This conversation is a direct message, which has no threads to "
                           "move between.", session=session)

    def refuse(status: str, message: str) -> ShareResult:
        _record(installation, user, status, message, {"channel": channel_id, "mode": "move",
                "session": str(session.pk)}, ok=False)
        return ShareResult(status, message, session=session)

    byline, caller_agent = _byline(installation, user, session)
    if not byline:
        return refuse(NOT_LINKED, (
            "Your canopy account isn't linked to a Slack user here. Mention @canopy once in Slack "
            "to link it, then try again."))
    agent = session.agent if session.agent_id else caller_agent
    target = f"`{session.agent.slug}`" if session.agent_id else "this session"
    text = (f"{byline} started a new thread for the conversation with {target} · "
            f"<{services.session_url(session)}|open in canopy>\n\n{relay.to_mrkdwn(summary.strip())}"
            "\n\n_Reply in this thread to talk to this session._")
    try:
        body = client.post_message_body(installation.bot_token, channel=channel_id, text=text,
                                        persona=relay.persona(agent))
    except client.SlackApiError as e:
        if e.error in _CANNOT_POST_THERE:
            return refuse(NOT_IN_CHANNEL, (
                f"canopy can't post in <#{channel_id}> any more ({e.error}). Invite the canopy app back "
                "to the channel and try again."))
        logger.exception("slack new-thread post failed")
        return refuse(POST_FAILED, f"Slack refused the post: {e.error}.")
    new_ts = str(body.get("ts") or "")

    now = time.time()
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(locked.metadata or {})
        old_key = meta.get(services.SLACK_THREAD_KEY)
        record = {
            "thread": old_key,
            "team": str(meta.get("slack_team") or ""),
            "channel": channel_id,
            "thread_ts": old_ts,
            "shared_by": meta.get("slack_shared_by"),
            "bound_at": meta.get(relay.BOUND_AT),
            "detached_at": now,
            "detached_by": user.pk,
            "moved_to": new_ts,
        }
        shared_by = meta.get("slack_shared_by")
        for k in _BINDING_KEYS:
            meta.pop(k, None)
        meta.update({
            services.SLACK_THREAD_KEY: services.thread_key(installation.team_id, channel_id, new_ts),
            "slack_team": installation.team_id,
            "slack_channel": channel_id,
            "slack_thread_ts": new_ts,
            # Nothing said before the move is the new thread's business.
            relay.ELSEWHERE_AT: now,
            relay.BOUND_AT: now,
        })
        if shared_by:
            meta["slack_shared_by"] = shared_by
        meta[DETACHED_KEY] = (list(meta.get(DETACHED_KEY) or []) + [record])[-_DETACHED_HISTORY:]
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = meta

    from .models import SlackMenuPost

    # An open question goes with the conversation: its post is keyed per
    # (session, question), so it is re-posted into the new thread, not left behind.
    SlackMenuPost.objects.filter(session=session).delete()

    new_link = client.permalink(installation.bot_token, channel=channel_id, ts=new_ts)
    location.remember(session, channel_name=str(before.get("channel_name") or ""), permalink=new_link)
    where = new_link or location.web_url(installation.team_id, channel_id, new_ts)
    _close_old_thread(installation, session, channel_id, old_ts, where, agent)

    message = f"Moved this conversation to a new thread: {where}. Replies there now reach this session."
    _record(installation, user, MOVED, message, {"channel": channel_id, "ts": new_ts, "mode": "move",
            "from_ts": old_ts, "session": str(session.pk)}, ok=True, kind="slack.moved")
    return ShareResult(MOVED, message, session=session, channel_id=channel_id, ts=new_ts,
                       permalink=where, channel_name=str(before.get("channel_name") or ""),
                       previous={"channel": channel_id, "thread_ts": old_ts,
                                 "permalink": before.get("permalink") or ""})


def _close_old_thread(installation, session: Session, channel_id: str, old_ts: str, where: str,
                      agent) -> None:
    """Point the old thread at the new one. Each step best-effort: the move has happened."""
    from apps.harness.models import Turn

    from .models import SlackTurnPost

    try:
        client.post_message(installation.bot_token, channel=channel_id, thread_ts=old_ts,
                            text=MOVED_NOTE.format(where=where), persona=relay.persona(agent))
    except Exception:  # noqa: BLE001
        logger.exception("could not post the Slack moved note")
    try:
        client.set_session_status(installation.bot_token, channel=channel_id, thread_ts=old_ts,
                                  status=client.ACTIVE)
    except Exception:  # noqa: BLE001
        logger.exception("could not clear the Slack agent-session status")
    # A turn still in flight stops reaching the old thread (relay.predates_bind),
    # so its status line there would read "working…" forever. Close it off.
    posts = SlackTurnPost.objects.filter(turn__chat_session=session, channel_id=channel_id,
                                         turn__status__in=list(Turn.NON_TERMINAL)).exclude(slack_ts="")
    for post in posts:
        try:
            client.update_message(installation.bot_token, channel=channel_id, ts=post.slack_ts,
                                  text=f"Moved to a new thread: {where}")
        except Exception:  # noqa: BLE001
            logger.exception("could not close a Slack status line after a move")
