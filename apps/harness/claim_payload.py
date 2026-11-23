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

from django.conf import settings


def issue_credentials(turn):
    """Mint what only the claiming runner may hold, onto the turn object:
    a chat's key, and a confined turn's caller token — plus the model its CLI
    starts on (`cli_model`), which both claim channels send. Returns the turn."""
    agent = turn.acting_agent
    turn.cli_model = conversation_model(turn, agent)
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


def start_model(agent, cli: str | None) -> str:
    """The model a turn's CLI starts on: its agent's `cli_models` entry for that CLI,
    else the server's `DEFAULT_CLI_MODELS` for it, else "" (the CLI's own default on
    that box, which is whatever the box's account defaults to, not what a person
    gets on their own machine). A project's turn has no agent, so it always takes
    the server default."""
    cli = cli or "claude-code"
    named = (agent.cli_models or {}).get(cli) if agent else None
    return str(named or (settings.DEFAULT_CLI_MODELS or {}).get(cli) or "")[:64]


def page_model(turn, agent) -> str:
    """The model a turn held on a host site's page starts on when the agent's entry for
    that site names one (`page_model:`), else "". Claude Code's model names only: a
    turn on another CLI keeps that CLI's own."""
    if agent is None or (turn.cli or "claude-code") != "claude-code":
        return ""
    from apps.agents import published_profiles  # noqa: PLC0415 — the interface reads models, loaded with a call

    site = published_profiles.page_site_of(turn, agent)
    return published_profiles.page_model_of(agent, site)[:64] if site else ""


def conversation_model(turn, agent) -> str:
    """The model this turn starts on. A chat's turn takes the one its conversation
    already holds (`Session.cli_model`); the conversation's first claim picks it
    (`start_model`) and holds it, first write wins. So changing an agent's model or
    the server default moves new conversations, never one mid-history. Every turn
    still SENDS it: a runner starts a fresh CLI process per turn, which would
    otherwise start on its box's own default. A turn with no chat is not held.
    A conversation held on a site's page whose agent names a `page_model` for that
    site starts on it (`page_model`); every other turn keeps the agent's own."""
    session = turn.chat_session if turn.chat_session_id else None
    if session is not None and session.cli_model:
        return session.cli_model
    model = page_model(turn, agent) or start_model(agent, turn.cli)
    if session is not None and model:
        from apps.canopy_sessions.models import Session  # noqa: PLC0415 — sessions import harness

        if not Session.objects.filter(pk=session.pk, cli_model="").update(cli_model=model):
            # Another claim held one first: that one is the conversation's.
            model = Session.objects.filter(pk=session.pk).values_list("cli_model", flat=True).first() or model
        session.cli_model = model
    return model


def claim_payload(turn) -> dict:
    """The claimed turn as JSON, exactly as the REST claim route returns it."""
    from .schemas import ClaimedTurnOut

    return ClaimedTurnOut.from_orm(issue_credentials(turn)).model_dump(mode="json")
