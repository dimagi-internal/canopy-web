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
    _mark_author(turn)
    return turn


def _mark_author(turn) -> None:
    """Who wrote a chat line rides INSIDE the delivered prompt, because the
    transcript that comes back records only what the agent read (spec
    2026-09-26). Set on this in-memory instance only — never saved: Slack's
    status line and the lost-turn re-ask read Turn.prompt and must see the bare
    words. Here rather than in one channel's route so a WebSocket claim and a
    REST claim deliver the same prompt."""
    from apps.canopy_sessions.authorship import for_turn

    bare = turn.prompt or ""
    turn.prompt = for_turn(turn)
    if turn.prompt == bare:
        return
    # A laptop runner names a NEW emdash session from the prompt's first line
    # unless origin_ref carries a `subject` (session_naming's ladder). A runner
    # older than its marker-skipping rule would name every new chat
    # `c-canopy-from-<name>-user-…`, and canopy's Chats list copies that key
    # into the title. So hand it the bare first line — in memory, like the
    # prompt, and never over a subject the turn already had.
    ref = dict(turn.origin_ref or {})
    first = next((ln.strip() for ln in bare.splitlines() if ln.strip()), "")
    if first and not ref.get("subject"):
        ref["subject"] = first[:80]
        turn.origin_ref = ref


def claim_payload(turn) -> dict:
    """The claimed turn as JSON, exactly as the REST claim route returns it."""
    from .schemas import ClaimedTurnOut

    return ClaimedTurnOut.from_orm(issue_credentials(turn)).model_dump(mode="json")
