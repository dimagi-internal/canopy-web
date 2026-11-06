"""Take a channel thread's work offline, into a private DM thread, and report back.

A conversation with an agent in a channel thread starts shared and then, on long
work, drowns the channel. Taking it offline moves the WORK out and leaves the
conversation where it was:

* the person who asked gets a new thread in their direct messages with canopy,
  holding a child `Session` of the channel's (`lineage.BRANCH`: it starts from
  the channel conversation so far, condensed — `lineage.context_prompt`) that
  only they are in. The same session is open in canopy (the web chat, the IDE),
  so they can work on it from either;
* the channel thread gets one line saying who took it offline, and nothing more
  from the offline work. It stays a live conversation: anyone may still talk to
  the agent there;
* when they say so, the offline session reports its outcome back
  (`lineage.report_back`, the `/canopy-sessions/lineage/report-back` route): a
  turn in the channel conversation that posts it in the thread.

Started by `@canopy offline <ask>` in the thread, or by the agent itself when
asked in words (`take_session_offline`, run in the asker's name). Who may: anyone
who may write in the conversation, or an admin of its agent — the same people
who may branch it (apps/slack/branch.py), which is the channel-thread twin of
this.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.canopy_sessions import access as session_access
from apps.canopy_sessions import lineage
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session
from apps.canopy_sessions.services import add_runner_requirements
from apps.harness import runner_requirements as rr
from apps.harness import sensitivity

from . import client, location, relay
from .branch import thread_parent
from .services import (
    BLOCKED,
    EMPTY,
    MOVED,
    SENT,
    SLACK_THREAD_KEY,
    Inbound,
    Outcome,
    _link,
    _send,
    resolve_principal,
    session_url,
    thread_key,
)
from .share import _slack_user

logger = logging.getLogger(__name__)

#: `@canopy offline <ask>` inside a thread.
OFFLINE_WORD = "offline"
#: Session metadata on offline work: the channel thread key it was taken from.
OFFLINE_FROM_KEY = "slack_offline_from"

NOT_IN_A_THREAD = ("`@canopy offline` works inside a channel thread an agent is already in: it moves "
                   "the work to a private thread in your DMs and reports back here when you say so.")

OK, REFUSED, NOT_LINKED, FAILED = "ok", "refused", "not_linked", "failed"


@dataclass
class OfflineResult:
    status: str
    message: str
    #: HTTP status for a refusal on the API.
    http: int = 200
    session: Session | None = None
    outcome: Outcome | None = None
    dm_channel: str = ""
    dm_permalink: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK


def is_offline(text: str) -> tuple[bool, str]:
    """(this is `offline …`, the ask after the word)."""
    first, _, rest = text.strip().partition(" ")
    return first.lower().rstrip(":,.!") == OFFLINE_WORD, rest.strip()


def handle(installation, inbound: Inbound, ask: str) -> Outcome:
    """`@canopy offline <ask>`, typed in a channel thread."""
    parent = thread_parent(installation, inbound)
    if parent is None:
        return Outcome(EMPTY, NOT_IN_A_THREAD)
    principal, refusal = resolve_principal(installation, inbound.slack_user_id, parent.agent.workspace_id)
    if refusal is not None:
        return refusal
    result = take_offline(parent, principal.user, ask, slack_user_id=inbound.slack_user_id)
    if result.outcome is not None:
        return result.outcome
    return Outcome(BLOCKED if result.status == REFUSED else EMPTY, result.message, session=parent,
                   agent=parent.agent, workspace_id=parent.workspace_id)


def may_take_offline(user, parent: Session) -> bool:
    return user is not None and (session_access.can_write(user, parent) or parent.agent.is_admin(user))


def take_offline(parent: Session, user, ask: str = "", *, slack_user_id: str = "",
                 origin_turn=None) -> OfflineResult:
    """Move `parent`'s work to a private DM thread for `user`. `origin_turn` is the
    turn that asked (an agent acting on words), recorded as the child's parent turn."""
    dest = relay.session_destination(parent)
    if parent.agent_id is None or dest is None:
        return OfflineResult(REFUSED, "This conversation isn't in a Slack channel thread, so there "
                             "is nothing to take offline.", http=409)
    installation, channel_id, thread_ts = dest
    if not thread_ts or (location.location_of(parent) or {}).get("is_dm"):
        return OfflineResult(REFUSED, "This conversation is already a direct message.", http=409)
    if sensitivity.is_sensitive(parent):
        # Its words are sealed to its people; a Slack DM is not one of its chat surfaces.
        return OfflineResult(REFUSED, sensitivity.REFUSAL, http=403)
    agent = parent.agent
    if not may_take_offline(user, parent):
        return OfflineResult(REFUSED, "Only people in this conversation (its owner or an editor), or "
                             f"an admin of `{agent.slug}`, can take it offline.", http=403)
    if not slack_user_id:
        slack_user_id = _slack_user(installation, user)
    if not slack_user_id:
        return OfflineResult(NOT_LINKED, "That person's canopy account isn't linked to a Slack user "
                             "here, so there is no DM to move the work to.", http=409)
    principal, refusal = resolve_principal(installation, slack_user_id, agent.workspace_id)
    if refusal is not None or principal.user != user:
        return OfflineResult(NOT_LINKED, (refusal.message if refusal is not None else
                             "That Slack account belongs to someone else in canopy."), http=409)
    token = installation.bot_token
    title = (ask or parent.title or "Slack thread").strip()
    from_url = (client.permalink(token, channel=channel_id, ts=thread_ts)
                or location.web_url(installation.team_id, channel_id, thread_ts))
    try:
        dm = client.open_dm(token, slack_user_id)
        root_ts = client.post_message(token, channel=dm, persona=relay.persona(agent),
                                      text=_dm_text(title, from_url, agent.slug))
    except client.SlackApiError as e:
        logger.exception("could not open the offline DM thread")
        return OfflineResult(FAILED, f"Slack refused the direct message: {e.error}.", http=502)
    dm_key = thread_key(installation.team_id, dm, root_ts)
    child = session_services.create_session(
        workspace=parent.workspace, created_by=user, agent=agent, title=f"Offline: {title}"[:200],
        relation=lineage.BRANCH, parent={"session": parent, **({"turn": origin_turn} if origin_turn else {})},
        metadata={
            SLACK_THREAD_KEY: dm_key, "slack_team": installation.team_id, "slack_channel": dm,
            "slack_thread_ts": root_ts, OFFLINE_FROM_KEY: (parent.metadata or {}).get(SLACK_THREAD_KEY),
        })
    binding = getattr(parent, "runner_binding", None)
    if getattr(binding, "transcript_id", ""):
        child.parent_claude_session = binding.transcript_id[:100]
        child.save(update_fields=["parent_claude_session", "updated_at"])
    # It carries the parent's words on, so it is held to what the parent requires.
    add_runner_requirements(child, rr.requirements_of_session(parent))
    dm_link = client.permalink(token, channel=dm, ts=root_ts)
    try:
        # Now that the session exists, the DM post can name it exactly.
        client.update_message(token, channel=dm, ts=root_ts,
                              text=_dm_text(title, from_url, agent.slug, session_url(child)))
    except client.SlackApiError:
        logger.exception("could not name the offline session in its DM post")

    who = f"<@{slack_user_id}>"
    after = (
        f"You are now working OFFLINE with {user.get_full_name() or user.email}: in a private Slack "
        f"direct-message thread that only they are in, also open in canopy at {session_url(child)}. "
        "The channel thread this came from no longer sees this work. When they tell you to report "
        "back, call `report_back_to_parent` with your turn_id and the outcome — what was decided, "
        "built or changed, links, and what is still open — written for the people in the channel "
        "who did not follow the work. Pass `final: false` for an interim update. Don't report back "
        "unless they ask."
    )
    prompt = lineage.context_prompt(parent, ask, how="OFFLINE WORK taken", after=after)
    dm_inbound = Inbound(team_id=installation.team_id, channel_id=dm, slack_user_id=slack_user_id,
                         text=ask, ts=root_ts, thread_ts=root_ts, is_dm=True)
    outcome = _send(child, True, agent, principal, prompt, dm_inbound)
    if outcome.status != SENT:
        return OfflineResult(FAILED, outcome.message, http=409, session=child, outcome=outcome,
                             dm_channel=dm, dm_permalink=dm_link)
    try:
        client.post_message(token, channel=channel_id, thread_ts=thread_ts, persona=relay.persona(agent),
                            text=(f":arrow_heading_down: {who} took this offline with `{agent.slug}`"
                                  + (f": _{ask}_" if ask else "")
                                  + ". The work carries on privately; the outcome will be reported back here."))
    except client.SlackApiError:
        logger.exception("could not tell the channel thread its work went offline")
    message = (f"Took this offline: carry on in your DMs with canopy "
               f"{_link(dm_link, 'here') if dm_link else ''} or in canopy: {session_url(child)}. "
               "Say *report back* there when you want the outcome posted in this thread.")
    # MOVED, not SENT: the work went somewhere else, and the person who moved it is
    # told where, privately (a SENT ask is acknowledged by its status line instead).
    outcome.status, outcome.message = MOVED, message
    outcome.extra = {**outcome.extra, "offline_from": str(parent.pk)}
    return OfflineResult(OK, message, session=child, outcome=outcome, dm_channel=dm, dm_permalink=dm_link)


def _dm_text(title: str, from_url: str, agent_slug: str, canopy_url: str = "") -> str:
    """The post that opens the offline thread in the person's DMs."""
    there = f", or open it in canopy: {canopy_url}" if canopy_url else ""
    return (f":lock: *Offline:* {title}\nTaken from {_link(from_url, 'a channel thread')} to work on "
            f"with `{agent_slug}` — only you are in this one. Reply in this thread to carry on{there}. "
            "When you're done, say *report back* and the outcome goes to the channel thread.")
