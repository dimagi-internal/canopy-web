"""Moving an agent (apps/agents/move.py) and its Slack channel ingests.

An ingest's approval was given by an admin of the workspace the agent was in,
under THAT workspace's `channel_ingest_enabled` — neither is a fact about the
destination. So a move revokes every pending or live ingest (reads stop, the
transcript is deleted, the approval post says so, and it is audited where it
was granted) and then re-homes the rows, ended ones included, with the agent.
The agent asks again from its new home if it still needs the channel.

Its allow-list entries go too: an owner of the OLD workspace gave them, so they
are deleted, not carried — the new workspace's owner allow-lists afresh.
"""
from __future__ import annotations

from apps.agents.move import Plan

from . import ingest
from .models import SlackChannelIngest, SlackChannelIngestAllow


def participant(p: Plan, apply: bool) -> None:
    agent, dest = p.agent, p.dest
    rows = SlackChannelIngest.objects.filter(agent=agent).exclude(workspace_id=dest.slug)
    open_ = list(rows.filter(status__in=(SlackChannelIngest.PENDING, *ingest.LIVE))
                 .select_related("agent", "installation", "workspace"))
    for i in open_:
        p.breaks("slack_channel_ingest",
                 f"the {i.status} read of Slack channel {i.channel_name or i.channel_id} is revoked: its "
                 f"approval was given in '{i.workspace_id}', not '{dest.slug}'. Ask again after the move")
        if apply:
            ingest.revoke(i, user=p.by)
    n = rows.update(workspace=dest) if apply else rows.count()
    p.moves("slack.SlackChannelIngest", n, "re-homed; pending and live ones revoked first")
    allows = SlackChannelIngestAllow.objects.filter(agent=agent).exclude(workspace_id=dest.slug)
    for a in allows:
        p.breaks("slack_channel_allow",
                 f"the allow-list entry for Slack channel {a.channel_name or a.channel_id} is removed: an owner "
                 f"of '{a.workspace_id}' gave it. An owner of '{dest.slug}' can allow-list it again")
    gone = allows.count()
    if apply:
        allows.delete()
    p.moves("slack.SlackChannelIngestAllow", gone, "deleted, not carried: granted by the old workspace's owner")
