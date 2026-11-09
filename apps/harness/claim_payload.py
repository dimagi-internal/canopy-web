"""What a runner receives when it claims a turn — ONE payload, whichever channel.

A runner claims over REST (`POST /api/harness/runners/{id}/claim`) or over its
WebSocket control channel (`RunnerConsumer`, the cloud runner's normal path).
Both build the answer the same way: the REST route's `ClaimedTurnOut`, a
confined turn's caller token and a chat's key. A hand-picked subset on the
socket (no caller envelope, no caller token, no chat key) would be wrong: the
runner decides whether to CONFINE a turn from that envelope, so a confined turn
claimed over the socket would look ordinary and run in the agent's full profile.

So both channels call `claim_payload`, and anything a claim must carry is added
here, once. The credentials are minted per claim and exist only in this answer.
"""
from __future__ import annotations


def issue_credentials(turn):
    """Mint what only the claiming runner may hold, onto the turn object:
    a chat's key, and a confined turn's caller token — plus the model its CLI
    starts on (`cli_model`), which both claim channels send. Returns the turn."""
    agent = turn.acting_agent
    turn.cli_model = str((agent.cli_models or {}).get(turn.cli) or "")[:64] if agent else ""
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
    else:
        # A FULL turn asked by someone who is not the agent's owner, an admin or
        # canopy (an editor) is not confined, but must still read
        # canopy as its asker rather than as the runner's owner.
        # A project's turn (no agent) asked by a verified person reads canopy as
        # them too: it has no agent to act as (see access-confinement-and-act-as.md).
        from .caller_tokens import is_caller_scoped, mint

        if is_caller_scoped(turn):
            turn.mcp_token = mint(turn, scoped=True)
    return turn


def claim_payload(turn) -> dict:
    """The claimed turn as JSON, exactly as the REST claim route returns it."""
    from .schemas import ClaimedTurnOut

    return ClaimedTurnOut.from_orm(issue_credentials(turn)).model_dump(mode="json")
