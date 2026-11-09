"""Which runners a request may see or act on — the predicates and the 404 resolver the
runner routes share. A non-route module, so `apps/agents` and the other apps that
resolve a runner import it without importing `harness.api` (the router)."""
from __future__ import annotations

import uuid

from django.db.models import Q
from django.http import HttpRequest
from ninja.errors import HttpError

from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import routing
from .models import Runner
from .runner_protocol import owned_runner_q


def _runner_owned_q(request: HttpRequest) -> Q:
    """Ownership: the caller owns it. A runner NOBODY owns is acted on by
    nobody (a NULL owner must not mean "acted on by everyone": heartbeat, claim, retire, and
    its plaintext credential bundle), the NULL-means-allow shape this repo has
    removed everywhere else."""
    return owned_runner_q(request.user)


#: Fleet runners only: a computer's key holder (`Runner.COMPUTER` — a person's
#: computer in plain Claude Code, no runner app) is not a box anyone lists, pins,
#: heartbeats, claims as or administers; its keys are reached through
#: `/api/runner-device-keys/` and its machine through `/api/private-runners/`.
_FLEET_Q = ~Q(kind=Runner.COMPUTER)


def runner_read_q(request: HttpRequest) -> Q:
    """'Runners this caller can SEE' — derived from the TENANT, like claim time.

    Seeing a runner and acting on one are different questions that one predicate
    answered, and its `owner == caller` leg is right for the second and wrong
    for the first. A workspace's fleet is typically owned by ONE human, so every
    other member listed ZERO runners and could not distinguish "no runner serves
    this repo" from "I can see nothing at all".

    `canopy project dispatch` preflights by listing the fleet; under an agent
    identity an owner-only list comes back empty, the preflight concludes BLOCKED,
    and it gets routed around with `--no-preflight`. The next dispatch then goes at a
    repo nothing declared, is accepted 201, and sits QUEUED until the stuck-turn
    banner catches it — a guard that cries wolf gets disabled, and takes the true
    positives with it.

    `services.unclaimable_queued_turns` follows the same rule at its own call site:
    scoping candidates to `owner=user` would make every stuck turn read as `config`
    for anyone who had not personally paired a runner.

    What is deliberately NOT inherited from the act-on predicate: its
    `workspace_id__isnull=True` leg. There it is backstopped by ownership, so it
    means "your own legacy runner"; here, with ownership gone, the same leg would
    mean "everyone's", which is the NULL-means-allow shape this codebase has
    already removed from its tenancy predicates. A
    runner with no workspace has no tenant to share, so it stays visible only to
    its owner — hence the `& _runner_owned_q` on that leg alone.

    Nothing listed is secret to a member: RunnerOut carries status, capabilities,
    host and `owner_email` — never credentials, which have their own
    owner-gated route.
    """
    ws = getattr(request, "workspace_slug", None)
    if ws:
        # Tenant-pinned: homed here, or SERVING here (`_runners_serving_q`), and
        # no null-workspace leg at all — a null-workspace runner is wrong-tenant
        # here, not ungated (the property
        # test_pinned_null_workspace_runner_is_neither_listed_nor_actionable
        # pins). WorkspaceResolveMiddleware has already gated membership of `ws`.
        return (Q(workspace_id=ws) | _runners_serving_q({ws})) & _FLEET_Q
    slugs = wsvc.user_workspace_slugs(request.user)
    return (
        Q(workspace_id__in=slugs)
        | _runners_serving_q(slugs)
        | (Q(workspace_id__isnull=True) & _runner_owned_q(request))
    ) & _FLEET_Q


def _runners_serving_q(slugs) -> Q:
    """Runners the claim path would hand a turn in any of `slugs` — the tenant
    gate itself (`services.runner_tenant_slugs`: owner's `agent.work` ∩
    enrollment coverage), not `Runner.workspace`, which only records where a box
    LIVES. The fleet's cloud boxes live in `dimagi` and serve `connect`; an
    exact match on the home workspace listed them nowhere in `connect` and
    422'd a turn pinned to one there, while claim gave the same box the same
    turn unpinned.

    Read-only widening: everything that speaks FOR a runner still gates on
    `_runner_visibility_q` (ownership). A runner with no home workspace stays
    out, as the read predicate has always kept it; a NULL owner fails closed
    inside `runner_tenant_slugs`."""
    slugs = set(slugs)
    if not slugs:
        return Q(pk__in=[])
    candidates = (Runner.objects.exclude(status=Runner.RETIRED).filter(_FLEET_Q)
                  .filter(owner__isnull=False, workspace__isnull=False)
                  .select_related("owner").prefetch_related("enrollments"))
    tenants = routing.runner_tenant_slugs_of(candidates)
    return Q(pk__in=[pk for pk, served in tenants.items() if served & slugs])


def _runner_visibility_q(request: HttpRequest) -> Q:
    """'Runners this caller can ACT ON' — the tenant AND ownership. Unchanged.

    Heartbeating, claiming as, mutating, retiring and crediting a runner gate on
    this. Ownership is the boundary because these operations speak FOR the runner:
    `owner` is what `claim_next_turn` derives a tenant from, so acting as
    someone else's runner is acting with their memberships.

    `runner_read_q` is deliberately WIDER, which gives up the invariant these two
    would share by being one function ("never list a runner every action then
    404s on"). That invariant is now carried explicitly instead of structurally,
    by `RunnerOut.can_manage`: a client can say "runner X serves this repo, ask
    its owner to declare on it" rather than discovering ownership from a bare 404
    on an action it was told to try. Read ⊇ act-on holds by construction — every
    leg here appears in the read, ANDed with less.
    """
    ws = getattr(request, "workspace_slug", None)
    if ws:
        # This workspace or one above it. A division's agents run on boxes that
        # live in its parent (the fleet's do: they live in `dimagi`, the agents in
        # `connect`), so an exact match refused the caller's OWN box on every
        # tenant-scoped routing save — the agent's Settings page and the fleet
        # map — while the flat route took it. Ownership still applies below; this
        # widens only where the caller's own box may live, never whose it is.

        here = Workspace.objects.filter(slug=ws).first()
        wq = Q(workspace_id__in=[ws, *(here.ancestor_slugs() if here else [])])
    else:
        wq = Q(workspace_id__in=wsvc.user_workspace_slugs(request.user)) | Q(workspace_id__isnull=True)
    return wq & _runner_owned_q(request) & _FLEET_Q


def runner_or_404(
    request: HttpRequest, runner_id: uuid.UUID, *, include_retired: bool = False
) -> Runner:
    """Resolve a live runner via _runner_visibility_q — the same predicate
    list_runners filters on, so a runner that is listed is always one you can
    act on. Binding to runner.owner (not to a specific token) is
    deliberate: BearerTokenAuthMiddleware stamps request.user = token.user and
    discards which token was used, and PATs are rotated by design
    (canopy:canopy-web-pat-mint is documented "re-run to rotate"), so
    token-binding would break the runner on every rotation. Accepted residual:
    another token of the SAME user still works.

    `include_retired` is for the ONE operation that must reach a retired runner:
    un-retiring it. Everything else keeps 404ing, so retirement still means
    "invisible and inert" everywhere it matters.
    """
    qs = Runner.objects.all() if include_retired else Runner.objects.exclude(status=Runner.RETIRED)
    runner = qs.filter(_runner_visibility_q(request)).filter(pk=runner_id).first()
    if runner is None:
        raise HttpError(404, "runner not found")
    return runner
