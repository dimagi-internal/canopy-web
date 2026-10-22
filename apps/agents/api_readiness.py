"""/api/agents/{slug}/… — whether an agent can run: its boxes' bootstrap reports
and its setup checklist."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.harness.device_keys import runner_serves_agent

from . import services
from .gates import (
    agent_for_read,
)
from .schemas import (
    AgentSetupStepOut,
    BootstrapReportIn,
    BootstrapReportOut,
)
from .setup import setup_steps

router = Router(auth=session_auth, tags=["agents"])


@router.get("/{slug}/readiness", response=list[BootstrapReportOut],
            summary="What each BOX reports it could actually materialize")
def agent_readiness(request: HttpRequest, slug: str) -> list[BootstrapReportOut]:
    """The counterpart to `credentials/status`, and the difference is the point.

    `status` answers "is the credential stored here". This answers "could the
    box USE it" — and on 2026-09-07 those disagreed for a whole day: canopy-web
    held a valid gog-token while every gmail call on the box failed, because the
    OAuth client id+secret it needs alongside had not materialized. Nothing
    outside journald could see that.

    `mailbox_ok` alone repeated the same shape one layer up on 2026-09-08: it
    was TRUE for a day while every ACE email turn blocked at preflight, because
    the box verifies the client whose token AUTHENTICATES while a turn presents
    the client its config DECLARES. Read `turn_ready` for "can this agent run a
    turn"; `mailbox_ok` only says some client works.
    """
    agent = agent_for_read(request, slug)
    return [
        BootstrapReportOut(
            runner_name=r.runner_name, client_creds_ok=r.client_creds_ok,
            mailbox_ok=r.mailbox_ok, gog_client=r.gog_client,
            turn_client=r.turn_client, turn_ready=r.turn_ready,
            env_ok=r.env_ok, detail=r.detail, reported_at=r.reported_at,
        )
        for r in services.bootstrap_reports(agent)
    ]


@router.get("/{slug}/setup", response=list[AgentSetupStepOut],
            summary="What is left before this agent can do its job")
def agent_setup(request: HttpRequest, slug: str) -> list[AgentSetupStepOut]:
    """The setup checklist, in order: repository, GitHub identity, mailbox, vault (the
    vault before the mailbox for a zero-knowledge agent), runner, its first real skill (done in Claude Code — `command` starts that session), first
    turn. Each step reads the same check the system runs for that thing."""

    agent = agent_for_read(request, slug)
    return [AgentSetupStepOut(**s) for s in setup_steps(agent, viewer_is_admin=agent.is_admin(request.user))]


@router.post("/{slug}/bootstrap-report", response=BootstrapReportOut,
             summary="A box reports what it materialized for this agent")
def post_bootstrap_report(request: HttpRequest, slug: str,
                          payload: BootstrapReportIn) -> BootstrapReportOut:
    """Same gate as `credentials/resolve`: only a caller pairing a live runner
    this agent routes to may report for it. A readiness signal anyone could
    write is a readiness signal nobody can trust — and this one is meant to be
    trusted over the control plane's own record of what it stored.
    """
    # Membership + `caller_runs_agent`, and deliberately NOT `agent_for_work`
    # on top: `caller_runs_agent` is strictly tighter than any role check, so a
    # role gate adds no security here — but it does add a way for readiness
    # reporting to start 403-ing, when a runner's pairing human happens to hold
    # `viewer`. A machine saying "I could not materialize this" is the last
    # signal to lose, and reporting what a box observed is not reshaping the
    # agent.
    agent = agent_for_read(request, slug)
    if not services.caller_runs_agent(request.user, agent):
        raise HttpError(403, "no live runner you pair is assigned to this agent")
    # A runner token speaks for ONE box: it reports only for an agent that box
    # serves, never for the owner's other agents (same rule as credentials).
    rtok = getattr(request, "runner_token", None)
    if rtok is not None:

        if not runner_serves_agent(rtok.runner, agent):
            raise HttpError(403, f"{rtok.runner.name} does not run {agent.slug}")
    r = services.record_bootstrap_report(
        agent, runner_name=payload.runner_name,
        client_creds_ok=payload.client_creds_ok, mailbox_ok=payload.mailbox_ok,
        gog_client=payload.gog_client, detail=payload.detail,
        turn_client=payload.turn_client, turn_ready=payload.turn_ready,
        env_ok=payload.env_ok,
    )
    return BootstrapReportOut(
        runner_name=r.runner_name, client_creds_ok=r.client_creds_ok,
        mailbox_ok=r.mailbox_ok, gog_client=r.gog_client,
        turn_client=r.turn_client, turn_ready=r.turn_ready,
        env_ok=r.env_ok, detail=r.detail, reported_at=r.reported_at,
    )
