"""/api/harness runner routes: retire, unretire, pause and unpause.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.http import HttpRequest

from apps.api.route_gates import gate

from . import (
    services,
)
from .api import (  # noqa: E402
    router,
    runner_or_404,
)
from .api_runners import _with_roots
from .models import (
    Runner,
    RunnerAssignment,
    Turn,
    WorkspaceRunnerOrder,
)
from .schemas import (
    PauseIn,
    RetireOut,
    RunnerOut,
)


@router.post("/runners/{runner_id}/retire", response=RetireOut)
@gate("runner", setting=True)
def retire_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Retire a runner — a decommission, not a liveness state (see
    Runner.live_status). Idempotent by construction: _runner_or_404 already excludes
    retired runners, so retiring an already-retired runner 404s at lookup rather than
    no-opping here. Reversible via /unretire — but see the caveat below.

    Deletes the runner's RunnerAssignment rows in the same transaction. A
    retired runner is invisible to _runner_visibility_q, but its stale
    assignment rows were NOT — GET /agents/{slug}/runners kept listing them,
    and PUT /agents/{slug}/runners round-trips that same list to save any
    unrelated change, so a lingering row 422'd every matrix save with "unknown
    or retired runner id" (a prod incident 2026-07-25). Ranks of the
    survivors need not be compacted — RunnerAssignment.rank is only ever
    compared relatively (0 = first choice), never assumed contiguous.

    CAVEAT: /unretire brings the runner back but does NOT restore these deleted
    assignments — re-add it to the agents that should route to it via the
    matrix (PUT /agents/{slug}/runners). Restoring the runner's identity keeps
    its bindings/credentials; its routing membership is intentionally not
    resurrected, since the fleet may have moved on while it was retired."""
    runner = runner_or_404(request, runner_id)
    with transaction.atomic():
        runner.status = Runner.RETIRED
        runner.save(update_fields=["status"])
        rows = list(RunnerAssignment.objects.filter(runner=runner).select_related("agent")
                    .order_by("agent__slug", "source", "actor"))
        RunnerAssignment.objects.filter(runner=runner).delete()
        # And out of every workspace's default order. Left in, it reads as a
        # listed runner the order's next save then 422s on, and every agent following that order would
        # silently lose the box.
        orders = [row.workspace for row in WorkspaceRunnerOrder.objects.filter(runner=runner)
                  .select_related("workspace")]
        WorkspaceRunnerOrder.objects.filter(runner=runner).delete()
    # Turns pinned to it would wait for it forever: nothing else may take a pin.
    for turn in Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner):
        services.finish_turn(turn, status=Turn.CANCELLED, allow_queued=True,
                             result_note=f"cancelled: {runner.name}, the runner it was pinned to, was retired")
    dropped = [{"agent": r.agent.slug, "source": r.source, "actor": r.actor} for r in rows]
    if orders:
        from apps.events import services as events

        for ws in orders:
            events.record([{
                "source": "harness.runners", "kind": "runner.route_dropped", "level": "warning",
                "summary": (f"{request.user.email} retired {runner.name}: it is no longer in "
                            f"{ws.slug}'s default runner order"),
                "payload": {"runner": str(runner.pk), "workspace_order": ws.slug},
            }], workspace=ws)
    if dropped and runner.workspace_id:
        from apps.events import services as events

        events.record([
            {"source": "harness.runners", "kind": "runner.route_dropped", "level": "warning",
             "summary": (f"{request.user.email} retired {runner.name}: {d['agent']} "
                         f"{d['source'] or 'default order'}"
                         + (f" for {d['actor']}" if d["actor"] else "")
                         + " no longer routes there"),
             "payload": {"runner": str(runner.pk), **d}}
            for d in dropped
        ], workspace=runner.workspace)
    return {"runner": runner.name, "dropped_routes": dropped}


@router.post("/runners/{runner_id}/unretire", response=RunnerOut)
@gate("runner", setting=True)
def unretire_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Bring a retired runner back, keeping its identity — and therefore every
    RunnerBinding, assignment and session that points at it.

    Retirement used to be impossible to undo, which made it a trap rather than a
    decision. `_runner_or_404` 404s a retired runner, so its daemon's heartbeat,
    claim and session-report calls all fail forever once retired; and `pair_runner`
    unconditionally CREATES a row, so the only recovery — re-pairing — minted a new
    id and orphaned the old one's bindings. Retiring a laptop you were logged out of
    therefore silently destroyed its sessions' identity the moment you brought it
    back (labs 2026-07-25: jj-mbp-cdp, 10 sessions).

    Restores DISCONNECTED, not ONLINE: liveness is observed, never asserted — the
    next heartbeat is what makes it online (Runner.live_status). Idempotent for an
    already-live runner.
    """
    runner = runner_or_404(request, runner_id, include_retired=True)
    if runner.status == Runner.RETIRED:
        runner.status = Runner.DISCONNECTED
        runner.save(update_fields=["status"])
    return _with_roots(request, runner)


@router.post("/runners/{runner_id}/pause", response=RunnerOut)
@gate("runner", setting=True)
def pause_runner(request: HttpRequest, runner_id: uuid.UUID, payload: PauseIn):
    """Stop ROUTING work to this runner, without decommissioning it.

    The remote half of the runner's local `~/.canopy/PAUSED` sentinel, which only
    a human on that machine could ever drop. Jonathan runs the fleet under two
    macOS accounts for token-limit failover (Runner.host), and moving work off a
    rate-limited account means silencing its runner FROM THE OTHER ONE — impossible
    until now, because ~/.canopy there is owned by the other account. The only
    reachable lever was `retire`, which is a decommission: it deletes
    RunnerAssignment rows `unretire` does not restore, and it 404s the daemon's own
    heartbeat and claim calls. This destroys nothing and is reversible by
    construction.

    ENFORCED SERVER-SIDE, so it does not depend on the runner cooperating or even
    being up to date: `live_status` reports PAUSED, and `claim_next_turn`'s first
    guard already refuses anything that is not ONLINE. A paused runner may poll as
    often as it likes and will simply never be handed a turn. That also means a
    pause takes effect against an OLD runner binary with no deploy on that box.

    It outranks a PIN. `claim_next_turn` returns before pin matching, deliberately:
    a pin is operator intent, but so is a pause, and it is the more specific and
    more recent one. Letting a pin resurrect a parked box would re-open the exact
    hole this closes — work landing on an account that must not spend tokens. A
    turn pinned to a paused runner stays QUEUED (queued turns never expire) and
    lands when it comes back.

    Pause stops STARTING work, never finishing it: an executing turn keeps its
    lease and reports completion normally, matching the local sentinel's behavior.

    Idempotent — pausing an already-paused runner refreshes the note and returns
    200 rather than erroring, so a retry after a dropped response is safe.

    `until` also schedules the unpause (`Runner.unpause_at`) — what a runner sends
    when its Claude subscription hits a usage cap, with the reset the CLI named,
    so the box is parked exactly as long as it cannot work and the server's sweep
    (`services.wake_due_runners`) lifts it. A scheduled unpause never weakens a
    stronger pause: it is not added to an operator's open-ended pause, and a
    second cap only ever moves it later. A pause without `until` cancels any
    scheduled unpause — the operator outranks the clock.
    """
    runner = runner_or_404(request, runner_id)
    services.pause(runner, note=payload.note or "", until=payload.until, by=request.user)
    return _with_roots(request, runner)


@router.post("/runners/{runner_id}/unpause", response=RunnerOut)
@gate("runner", setting=True)
def unpause_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Resume routing to a paused runner. The exact inverse of /pause — it clears
    the flag and nothing else, because /pause destroyed nothing to restore.

    Contrast `unretire`, which cannot undo its own side effects (the deleted
    assignment rows) and says so. That asymmetry is the whole argument for pause
    existing as its own verb rather than people reaching for retire.

    Does NOT assert liveness: the runner comes back to whatever its heartbeat says
    it is, exactly as `unretire` restores DISCONNECTED rather than ONLINE. Liveness
    is observed, never asserted. Idempotent on an already-running runner.
    """
    runner = runner_or_404(request, runner_id)
    services.unpause(runner)
    return _with_roots(request, runner)
