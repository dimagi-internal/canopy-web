"""What a runner receives when it claims a turn — ONE payload, whichever channel.

A runner claims over REST (`POST /api/harness/runners/{id}/claim`) or over its
WebSocket control channel (`RunnerConsumer`, the cloud runner's normal path).
They used to build the answer separately: the REST route serialized
`ClaimedTurnOut` and minted a confined turn's caller token and a chat's key,
while the socket sent its own hand-picked subset of the turn — no caller
envelope, no caller token, no chat key. The runner decides whether to CONFINE a
turn from that envelope, so a confined turn claimed over the socket arrived
looking ordinary and ran in the agent's full profile (found 2026-09-26, while a
chat key failed to reach cloud-ec2-1).

So both channels call `claim_payload`, and anything a claim must carry is added
here, once. The credentials are minted per claim and exist only in this answer.
"""
from __future__ import annotations


def issue_credentials(turn):
    """Mint what only the claiming runner may hold, onto the turn object:
    a chat's key, and a confined turn's caller token. Returns the turn."""
    if turn.chat_session_id:
        # The chat's key (canopy_sessions.ChatKey): the one credential that lets
        # the session driving this chat reach the chat's own secrets and page.
        from apps.canopy_sessions import chat_keys

        turn.chat_key = chat_keys.mint(turn.chat_session)
    if turn.capability:
        # A confined turn's credential for canopy's own MCP (models.CallerToken).
        # Deliberately nothing else: a visitor's host credential (host grant
        # contract v1) never rides the claim — the agent reaches the host through
        # canopy's own MCP (`site_call`), which attaches it server-side, so no
        # runner, where every session is one OS user, ever holds it.
        from .caller_tokens import mint

        turn.mcp_token = mint(turn)
    return turn


def claim_payload(turn) -> dict:
    """The claimed turn as JSON, exactly as the REST claim route returns it."""
    from .schemas import ClaimedTurnOut

    return ClaimedTurnOut.from_orm(issue_credentials(turn)).model_dump(mode="json")
