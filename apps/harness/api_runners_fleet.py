"""/api/harness runner routes: the fleet list, one runner, and capability reports.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.route_gates import gate

from . import (
    routing,
)
from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_or_404,
    runner_read_q,
)
from .api_runners import _open_sessions_by_runner, _with_roots
from .models import (
    SERVES,
    Runner,
)
from .schemas import (
    RunnerCapabilitiesIn,
    RunnerOut,
)


@router.get("/runners/", response=list[RunnerOut], summary="List the fleet I can see")
@gate("member")  # runner_read_q: the tenant's fleet
def list_runners(request: HttpRequest):
    """The supervisor's runner status, and the fleet read every preflight makes.

    Scoped by TENANT (`runner_read_q`), not by who owns what: a member who
    paired nothing used to list nothing, which reads identically to "this
    workspace has no runners" and is the wrong answer to draw a conclusion from.
    Each row carries `can_manage` for the ownership half. Retired runners are
    excluded at lookup, as everywhere else.
    """
    return _runner_rows(request, _visible_runners(request)[:50])


@router.get("/runners/{runner_id}/", response=RunnerOut, summary="One runner I can see")
@gate("member")  # runner_read_q: the tenant's fleet, as the list
def get_runner(request: HttpRequest, runner_id: uuid.UUID) -> RunnerOut:
    """One row of `GET /runners/` — the same visibility and the same per-caller flags —
    for a page about this runner (`/w/:workspace/runners/:id`). 404 for one the list
    would not show."""
    rows = _runner_rows(request, _visible_runners(request).filter(pk=runner_id))
    if not rows:
        raise HttpError(404, "runner not found")
    return rows[0]


def _visible_runners(request: HttpRequest):
    qs = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(runner_read_q(request))
        # What every RunnerOut row reads (`owner_email`, `paused_by_email`,
        # `private_runner_id`), joined here rather than fetched per row.
        .select_related("owner", "paused_by", "private_runner")
        .prefetch_related("declared_flags", "enrollments")
        .order_by(models.F("last_heartbeat_at").desc(nulls_last=True))
    )
    from apps.embedding import delegation

    offered = delegation.offered_for(request)
    if offered is not None:
        # A site sees only the runners that serve its own agents — enough for a
        # "continue on…" picker, and nothing about the rest of the fleet.
        following = {r.pk for r in qs if routing.agents_following_runner(r) & set(offered)}
        qs = qs.filter(Q(agent_assignments__agent_id__in=offered) | Q(pk__in=following)).distinct()
    return qs


def _runner_rows(request: HttpRequest, qs) -> list:
    rows = list(qs)
    routing.prime_dedicated_agents(rows)
    open_by_runner = _open_sessions_by_runner([r.pk for r in rows])
    administered = harness_runners.administered_runner_ids(request.user, rows)
    for r in rows:
        reasons = open_by_runner.get(r.pk, {})
        r.open_sessions = sum(n for n, _ in reasons.values())
        r.open_sessions_by_reason = {
            reason: {"count": n, "oldest_activity_at": oldest}
            for reason, (n, oldest) in sorted(reasons.items())
        }
        # Resolved here rather than in the schema because it is a property of the
        # (caller, runner) PAIR, and a Ninja resolver only sees the row.
        r.can_manage = r.owner_id in (request.user.id, None)
        # Administration is a WIDER tier than acting as the runner, so it gets
        # its own flag rather than overloading can_manage — the credentials block
        # and the drill panel are gated by different routes.
        r.can_administer = r.pk in administered
    return _with_roots(request, rows)


@router.patch("/runners/{runner_id}", response=RunnerOut)
@gate("runner", kind="runner-protocol")
def update_runner_capabilities(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCapabilitiesIn):
    """Replace a runner's capabilities (owner-gated via _runner_or_404).

    Capabilities are set at pairing and were otherwise immutable — the only way to
    add a capability to an existing runner was to re-pair, which mints a NEW runner
    and orphans the old one's RunnerBindings. This lets a paired runner opt into
    driving new agents in place. capabilities is a routing hint, not a security
    boundary (the workspace gates), so replacing it changes what the runner PULLS,
    never what it may reach.

    EXCEPT `projects`, which the runner now REPORTS on every heartbeat (spec
    2026-07-28). Accepting a hand-written value would be accepting a ghost edit:
    the next heartbeat overwrites it seconds later, so the caller sees a 200,
    believes the repo is declared, and dispatches into a hole. A 422 naming the
    real fix is the honest answer. `projects` is also PRESERVED across a write that
    omits it — it belongs to the runner now, so a capabilities PATCH must not drop
    it as a side effect.
    """
    runner = runner_or_404(request, runner_id)
    # A reported key sent back UNCHANGED is an echo, not a declaration: the page's
    # Slack & chat toggle round-trips the capabilities it read. Only a value that
    # differs from what the box reported is a hand edit to refuse.
    reported = ("projects", "profiles", "envelope", "midturn", "unprepared", "data_mode",
                "data_mode_hash", "claude_model", "clis", "clis_unknown")
    payload.capabilities = {k: v for k, v in payload.capabilities.items()
                            if not (k in reported and runner.capabilities.get(k) == v)}
    if "projects" in payload.capabilities:
        raise HttpError(
            422,
            "`projects` is reported by the runner, not set by hand — it is replaced "
            "on every heartbeat from what the box actually has. To make a repo "
            "routable, open it as a project in emdash on that runner (or set "
            "RUNNER_PROJECTS on a cloud runner). PATCH `agents`/`sessions` freely.",
        )
    for key in ("profiles", "envelope", "midturn", "unprepared", "data_mode", "data_mode_hash",
                "claude_model", "clis", "clis_unknown"):
        if key in payload.capabilities:
            # Reported, like `projects` — and here it is a SECURITY property: a hand
            # edit claiming a runner can confine a caller's session would route
            # confined turns to a box that runs them in the full profile. `clis`
            # carries the same numbers per agent CLI (`clis.reported` prefers it).
            raise HttpError(422, f"`{key}` is reported by the runner on every heartbeat, "
                                 "not set by hand.")
    caps = dict(payload.capabilities)
    for key in reported:
        if runner.capabilities.get(key) is not None:
            caps[key] = runner.capabilities[key]
    # What a runner serves (`capabilities.serves`) is SERVER-OWNED once it is set (`machine_pairing` records it
    # from the invitation a person approved): a write can neither replace nor clear
    # it, or an agent's runner could lift its own restriction. A box-sent value is
    # ignored; a runner with none may still declare one (a self-restriction).
    if runner.serves():
        caps[SERVES] = runner.serves()
    runner.capabilities = caps
    runner.save(update_fields=["capabilities"])
    return _with_roots(request, runner)
