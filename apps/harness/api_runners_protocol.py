"""/api/harness runner routes: the box's own protocol (heartbeat, refresh, usage, claim)."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Status

from apps.api.route_gates import gate
from apps.events import audit

from . import (
    claim,
    runner_projects,
    usage,
)
from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_admin_or_404,
    runner_or_404,
)
from .api_runners import _with_roots
from .schemas import (
    ClaimedTurnOut,
    HeartbeatIn,
    RunnerOut,
    UsageReportIn,
    UsageReportOut,
    UsageScopeOut,
)
from .session_sync import report_projects


@router.post("/runners/{runner_id}/heartbeat", response=RunnerOut)
@gate("runner", kind="runner-protocol")
def runner_heartbeat(request: HttpRequest, runner_id: uuid.UUID, payload: HeartbeatIn):
    from . import runner_protocol
    from .api_device_keys import waiting_approval_url

    runner = runner_or_404(request, runner_id)
    rtok = getattr(request, "runner_token", None)
    beat = runner_protocol.heartbeat(
        runner, payload, device_key=rtok is not None and rtok.runner_id == runner.pk)
    beat.key_waiting_url = waiting_approval_url(runner)
    beat.report_projects_wanted = report_projects(beat)
    # The repos a person asked this laptop to add (`runner_projects.owed`), after
    # discharging any its report just showed it holds.
    if runner_projects.can_add_projects(beat):
        beat.add_projects_wanted = runner_projects.owed(beat)
    return beat


@router.post("/runners/{runner_id}/refresh", response=RunnerOut)
@gate("runner-admin", setting=True)
def refresh_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Ask this runner to refresh itself at its next idle moment: re-run its
    bootstrap, which updates the canopy plugin and CLI, Claude Code, and each
    agent's provisioning, then restart. `refresh_pending` stays true until the
    runner reports a bootstrap newer than the request."""
    # The administer tier, not the act-as tier: this changes what the box runs
    # on, the same class of operation as setting its credentials.
    runner = runner_admin_or_404(request, runner_id)
    return _with_roots(request, harness_runners.request_refresh(runner, by=request.user, via=audit.request_via(request)))


@router.get("/runners/{runner_id}/usage-scope", response=UsageScopeOut)
@gate("runner")
def runner_usage_scope(request: HttpRequest, runner_id: uuid.UUID):
    """The repos this box scans for its usage report: its declared projects and the
    agents it serves."""
    runner = runner_or_404(request, runner_id)
    return UsageScopeOut(repos=usage.scope(runner))


@router.post("/runners/{runner_id}/usage", response=UsageReportOut)
@gate("runner", kind="runner-protocol")
def post_runner_usage(request: HttpRequest, runner_id: uuid.UUID, payload: UsageReportIn):
    """The box's usage report: per Claude Code session, per UTC day, tokens and a
    little identity — never content. Each session's days replace what was held.
    Matched to canopy's own record of each session and counted there; one canopy
    has no record of is not kept."""
    runner = runner_or_404(request, runner_id)
    return UsageReportOut(**usage.record(runner, [s.model_dump() for s in payload.sessions]))


@router.post("/runners/{runner_id}/claim", response={200: ClaimedTurnOut, 204: None})
@gate("runner", kind="runner-protocol", step_up="runner protocol")
def claim_turn(request: HttpRequest, runner_id: uuid.UUID, paused: str = ""):
    """Claim the next eligible turn. `paused` is an optional comma-separated list of
    agent slugs the caller has locally paused (per-agent pause) — the server skips
    their queued turns so nothing is claimed-then-released. Omitted by older runners
    (backward-compatible: no exclusions). `X-Canopy-Acts-As: editor|viewer` claims in
    that lower mode: a turn the runner's owner started runs as that role."""
    runner = runner_or_404(request, runner_id)
    exclude = [s for s in (p.strip() for p in paused.split(",")) if s]
    from apps.workspaces import role_cap

    # A runner may claim in a lower mode it put itself in (X-Canopy-Acts-As).
    turn = claim.claim_next_turn(runner, exclude_slugs=exclude or None,
                                    acts_as=role_cap.header_cap(request))
    if turn is None:
        return Status(204, None)
    # The same credentials the WebSocket claim carries — see `claim_payload.py`,
    # which exists because the two channels once disagreed about them.
    from .claim_payload import issue_credentials

    return Status(200, issue_credentials(turn))
