"""`@canopy branch <ask>` — split a Slack thread's conversation into a new thread.

The new thread is a conversation of its own: its own Slack thread (a new
top-level message in the channel), its own canopy `Session` (child of the one it
branched from, `parent_session`) and so its own runner session and worktree. It
starts from the parent's conversation so far — condensed into its first prompt
(`exports.build_branch_seed`) — and goes its own way from there, while the parent
carries on untouched. Branch as often as you like: each is another sibling.

Who may branch: the person who started the conversation, or an admin of its
agent — the same people who may take its conversation elsewhere (`export_session`
is the starter's call). Anyone else is told so: a branch hands the whole
conversation to a new session, which is not something a reply in the thread
should be able to do.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from apps.canopy_sessions import access as session_access
from apps.canopy_sessions import lineage
from apps.canopy_sessions.models import REVIEW_METADATA_KEY, Session
from apps.canopy_sessions.services import add_runner_requirements
from apps.harness import runner_requirements as rr

from . import client
from .services import (
    BLOCKED,
    EMPTY,
    SENT,
    SLACK_THREAD_KEY,
    Inbound,
    Outcome,
    _link,
    _principal_name,
    _send,
    private_refusal,
    resolve_principal,
    tenant_sessions,
    thread_key,
    thread_session,
)

logger = logging.getLogger(__name__)

#: `@canopy branch <ask>` inside a thread.
BRANCH_WORD = "branch"
#: Session metadata on a branch: the thread key it was branched from.
BRANCHED_FROM_KEY = "slack_branched_from"
NOT_IN_A_THREAD = ("`@canopy branch` works inside a thread an agent is already in: it starts a "
                   "new thread that carries that conversation on separately.")


def is_branch(text: str) -> tuple[bool, str]:
    """(this is `branch …`, the ask after the word)."""
    first, _, rest = text.strip().partition(" ")
    return first.lower().rstrip(":,.!") == BRANCH_WORD, rest.strip()


def thread_parent(installation, inbound: Inbound, agent=None) -> Session | None:
    """The live agent conversation in the channel thread this message is in, or None
    (a DM, a top-level message, a thread no agent is in). `agent` — named in the
    message (`@canopy ace branch`) — picks that agent's conversation; unnamed, the
    thread's latest one."""
    if inbound.is_dm or not inbound.thread_ts:
        return None
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    sessions = (tenant_sessions(installation).select_related("agent")
                .filter(agent__isnull=False, **{f"metadata__{SLACK_THREAD_KEY}": key})
                .exclude(status=Session.ARCHIVED))
    if agent is not None:
        sessions = sessions.filter(agent=agent)
    return sessions.order_by("-created_at").first()


def not_here(agent, word: str) -> str:
    """Said when `@canopy <agent> <word>` names an agent with no conversation in the thread."""
    return (f"`{agent.slug}` has no conversation in this thread to {word}. Use `@canopy {word}` "
            "inside a thread an agent is already in.")


def handle(installation, inbound: Inbound, ask: str, agent=None) -> Outcome:
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    parent = thread_parent(installation, inbound, agent)
    if parent is None:
        if agent is not None and inbound.thread_ts and not inbound.is_dm:
            return Outcome(EMPTY, not_here(agent, BRANCH_WORD), agent=agent)
        return Outcome(EMPTY, NOT_IN_A_THREAD)
    # A branch carries a private conversation's key and history on: never from Slack.
    refused = private_refusal(parent)
    if refused is not None:
        return refused
    agent = parent.agent
    principal, refusal = resolve_principal(installation, inbound.slack_user_id, agent.workspace_id)
    if refusal is not None:
        return refusal
    user = principal.user
    # Anyone who may write into the conversation may branch it: they could already
    # start a new one and paste it all in. A viewer, or a member who never joined
    # the thread, may not.
    if user is None or not (session_access.can_write(user, parent) or agent.is_admin(user)):
        return Outcome(BLOCKED, "Only people in this conversation (its owner or an editor), or an "
                       f"admin of `{agent.slug}`, can branch it.", session=parent, agent=agent,
                       workspace_id=agent.workspace_id)
    token = installation.bot_token
    who = _principal_name(principal, inbound.slack_user_id)
    from_url = client.permalink(token, channel=inbound.channel_id, ts=inbound.ts)
    ask_line = f": _{ask}_" if ask else "."
    root_ts = client.post_message(token, channel=inbound.channel_id, text=(
        f":twisted_rightwards_arrows: *{who}* branched a conversation with `{agent.slug}` "
        f"{_link(from_url, 'from another thread')}{ask_line} It carries on here, separately — "
        "reply in this thread to continue it."))
    branch_inbound = replace(inbound, thread_ts=root_ts, follow=False, adopt_ts="", adopt_prefix="")
    branch, _ = thread_session(agent=agent, principal=principal,
                               key=thread_key(inbound.team_id, inbound.channel_id, root_ts),
                               inbound=branch_inbound,
                               title=f"Branch: {ask or parent.title or 'Slack thread'}")
    binding = getattr(parent, "runner_binding", None)
    branch.metadata = {**(branch.metadata or {}), BRANCHED_FROM_KEY: key}
    # A branch of a review chat carries what was read, so it stays in the review's profile.
    if REVIEW_METADATA_KEY in (parent.metadata or {}):
        branch.metadata[REVIEW_METADATA_KEY] = parent.metadata[REVIEW_METADATA_KEY]
    branch.save(update_fields=["metadata", "updated_at"])
    lineage.link(branch, parent, relation=lineage.BRANCH,
                 claude_session=getattr(binding, "transcript_id", "") or "")
    # The branch carries the parent's words on (its seed), so it is held to what the
    # parent requires — sensitive before its first message, never only by a link.
    add_runner_requirements(branch, rr.requirements_of_session(parent))

    prompt = lineage.context_prompt(parent, ask, how="a BRANCH")
    outcome = _send(branch, True, agent, principal, prompt, branch_inbound)
    if outcome.status == SENT:
        to_url = client.permalink(token, channel=inbound.channel_id, ts=root_ts)
        try:
            client.post_message(token, channel=inbound.channel_id, thread_ts=inbound.thread_ts, text=(
                f":twisted_rightwards_arrows: *{who}* branched this conversation into "
                f"{_link(to_url, 'a new thread')}{ask_line} This thread carries on as before."))
        except client.SlackApiError:
            logger.exception("could not point the original Slack thread at its branch")
        outcome.extra = {**outcome.extra, "branched_from": str(parent.pk)}
    return outcome

