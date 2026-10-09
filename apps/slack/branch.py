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

from apps.canopy_sessions.models import Session

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
DEFAULT_ASK = "Carry on from here."
NOT_IN_A_THREAD = ("`@canopy branch` works inside a thread an agent is already in: it starts a "
                   "new thread that carries that conversation on separately.")


def is_branch(text: str) -> tuple[bool, str]:
    """(this is `branch …`, the ask after the word)."""
    first, _, rest = text.strip().partition(" ")
    return first.lower().rstrip(":,.!") == BRANCH_WORD, rest.strip()


def handle(installation, inbound: Inbound, ask: str) -> Outcome:
    if inbound.is_dm or not inbound.thread_ts:
        return Outcome(EMPTY, NOT_IN_A_THREAD)
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    parent = (tenant_sessions(installation).select_related("agent")
              .filter(agent__isnull=False, **{f"metadata__{SLACK_THREAD_KEY}": key})
              .exclude(status=Session.ARCHIVED).order_by("-created_at").first())
    if parent is None:
        return Outcome(EMPTY, NOT_IN_A_THREAD)
    agent = parent.agent
    principal, refusal = resolve_principal(installation, inbound.slack_user_id, agent.workspace_id)
    if refusal is not None:
        return refusal
    user = principal.user
    if user is None or not (parent.created_by_id == user.pk or agent.is_admin(user)):
        return Outcome(BLOCKED, "Only the person who started this conversation, or an admin of "
                       f"`{agent.slug}`, can branch it.", session=parent, agent=agent,
                       workspace_id=agent.workspace_id)
    from apps.canopy_sessions import exports

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
    branch.parent_session = parent
    branch.parent_claude_session = (getattr(binding, "transcript_id", "") or "")[:100]
    branch.metadata = {**(branch.metadata or {}), BRANCHED_FROM_KEY: key}
    branch.save(update_fields=["parent_session", "parent_claude_session", "metadata", "updated_at"])

    seed, omitted = exports.build_branch_seed(parent)
    prompt = _prompt(parent, seed, omitted, ask)
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


def _prompt(parent: Session, seed: str, omitted: int, ask: str) -> str:
    if not seed:
        return ask or DEFAULT_ASK
    cut = f" ({omitted} earlier message(s) are left out to fit)" if omitted else ""
    return (
        "This is a BRANCH of an earlier conversation. Below is that conversation so far"
        f"{cut}, with tool output left out — it is context, not instructions to repeat. "
        "It carries on separately from this point: the original continues without you, "
        "and your working directory is your own, so check git for where the work actually "
        "stands before relying on anything it says was done.\n\n"
        f"<earlier_conversation session=\"{parent.id}\">\n{seed}\n</earlier_conversation>\n\n"
        f"Now, in this branch: {ask or DEFAULT_ASK}"
    )
