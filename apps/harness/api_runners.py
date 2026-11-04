"""/api/harness runner routes: pairing, and the helpers the other runner routes share. The routes are split by area into the `api_runners_*` modules, imported at the bottom of this one in route order (so the schema and the MCP tool names are unchanged); all register on the harness router (`api.router`).
"""
from __future__ import annotations

import logging
import uuid

from django.db import models
from django.http import HttpRequest
from ninja import Status
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.canopy_sessions import transcript_read
from apps.canopy_sessions.models import Session
from apps.canopy_sessions.staleness import active, open_reason
from apps.workspaces import services as wsvc

from .api import (  # noqa: E402
    declared_capabilities,
    router,
    runner_admin_or_404,
)
from .models import (
    Runner,
)
from .schemas import (
    RunnerIn,
    RunnerOut,
)

logger = logging.getLogger(__name__)


#: Published operationIds keep the label of the module these routes moved out of
#: (apps/api/api.py `_app_label`): an operationId is an MCP tool name.
OPERATION_LABEL = "harness"


@router.post("/runners/", response={201: RunnerOut})
# member of the explicit/default workspace; any role
@gate(
    "member",
    setting=True,
    viewer_write=(
        "pairing grants nothing by itself: a box serves only workspaces it is enrolled in (or below one) AND where its"
        " owner holds agent.work (runner_tenant_slugs); pairing enrolls it in the pairing workspace only"
    ),
)
def pair_runner(request: HttpRequest, payload: RunnerIn):
    if payload.kind not in dict(Runner.KIND_CHOICES):
        raise HttpError(422, f"unknown runner kind '{payload.kind}'")
    # A runner is OWNED by a person. `owner` is the box's identity for life —
    # its claims run with the owner's memberships and only the owner may grant
    # administration — so pairing with an agent's token makes a box nobody owns:
    # the human at the keyboard can't manage it, and their own work on it is
    # attributed to the agent. Measured 2026-10-02 (sarveshtewari-mbp-cdp, paired
    # as ace@dimagi-ai.com). An agent may still ADMINISTER a box through a grant.
    agent = getattr(request.user, "agent_identity", None)
    if agent is not None:
        raise HttpError(
            403,
            f"{request.user.email} is the login of agent '{agent.slug}', and a runner must "
            "be owned by a person — pair with YOUR canopy-web token (the "
            "canopy:canopy-web-pat-mint skill writes ~/.claude/canopy/workbench-token); "
            "the agent can then be granted admin via POST /api/harness/runners/{id}/admins",
        )
    explicit = (payload.workspace or "").strip()
    if explicit:
        # Membership-gated: a missing workspace and a non-member get the same
        # 404 (no existence leak), exactly as apps/agents does on explicit homing.
        if not wsvc.is_member(request.user, explicit):
            raise HttpError(404, f"workspace '{explicit}' not found")
        ws_slug = explicit
    else:
        # A runner MUST belong to a workspace: a workspace-less one is
        # half-broken with no signal — heartbeat and claim work (tenancy
        # derives from owner), but every session report 404s, so its
        # sessions silently never surface (prod incident 2026-07-25: a
        # multi-workspace owner made user_default_workspace() None and the
        # runner paired NULL). Fail loud instead of pairing broken.
        default = wsvc.user_default_workspace(request.user)
        if default is None:
            n = len(wsvc.user_workspace_slugs(request.user))
            detail = (
                "you belong to no workspace" if n == 0
                else f"you belong to {n} workspaces, so there is no default"
            )
            raise HttpError(
                422,
                f"a runner must belong to a workspace and {detail} — "
                "pass `workspace` explicitly",
            )
        ws_slug = default.slug
    runner = Runner.objects.create(
        name=payload.name,
        kind=payload.kind,
        capabilities=declared_capabilities(payload.capabilities),
        host=payload.host,
        owner=request.user,
        workspace_id=ws_slug,
    )
    return Status(201, _with_roots(request, runner))


def _list_admin_or_404(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    """`runner_admin_or_404`, for a route that changes the runner's credential list —
    which, for a runner on a credential set, is every runner on the set: then only the
    set's owner changes it, since administering one of its runners administers none of
    the others."""
    runner = runner_admin_or_404(request, runner_id)
    if runner.credential_set_id and runner.credential_set.owner_id != request.user.pk:
        raise HttpError(403, f"{runner.name} reads the shared list {runner.credential_set.name}; "
                             "only its owner changes it")
    return runner


def _open_sessions_by_runner(runner_ids) -> dict:
    """{runner id: {reason: (count, oldest activity)}} for the sessions open on it:
    active and still reported, the session list's own `state=active` rule
    (`unseen_q`), so the two counts cannot drift; each labelled by
    `staleness.open_reason`, which reads the same checks."""
    if not runner_ids:
        return {}
    rows = (
        transcript_read.with_activity(
            active(Session.objects.filter(runner_binding__runner_id__in=runner_ids))
        )
        .annotate(_open_reason=open_reason())
        .values("runner_binding__runner_id", "_open_reason")
        .annotate(n=models.Count("pk", distinct=True), oldest=models.Min("_activity"))
    )
    out: dict = {}
    for row in rows:
        out.setdefault(row["runner_binding__runner_id"], {})[row["_open_reason"]] = (
            row["n"], row["oldest"],
        )
    return out


def _with_roots(request: HttpRequest, runners):
    """Stamp each RunnerOut row's `roots` for this caller — the ONE helper every
    route returning a RunnerOut passes its runner(s) through (the schema serves
    [] for an unstamped row, so a route that forgets reveals nothing).

    The owner sees every tree the box is enrolled in. Anyone else sees a tree's
    root only when they are a member somewhere in that tree (a member of only
    `connect` sees `dimagi` for a box serving `connect`). One tree walk for all
    the rows; it also fills the cache `spans_trees` reads."""
    from .routing import enrolled_slugs

    rows = [runners] if isinstance(runners, Runner) else list(runners)
    if not rows:
        return runners
    enrolled = {r.pk: enrolled_slugs(r) for r in rows}
    mine = wsvc.user_workspace_slugs(request.user)
    roots = wsvc.tree_roots(set().union(*enrolled.values()) | mine)
    my_roots = {roots[s] for s in mine}
    for r in rows:
        all_roots = sorted({roots[s] for s in enrolled[r.pk]})
        r._enrolled_roots_cache = all_roots
        r.visible_roots = (all_roots if r.owner_id == request.user.id
                           else [root for root in all_roots if root in my_roots])
    return runners


# The routes, by area. Imported here, in this order, because the router (and so the
# OpenAPI document and the MCP tool list) follows registration order.
# isort: off
from . import api_runners_credentials  # noqa: E402,F401
from . import api_runners_mints  # noqa: E402,F401
from . import api_runners_access  # noqa: E402,F401
from . import api_runners_settings  # noqa: E402,F401
from . import api_runners_fleet  # noqa: E402,F401
from . import api_runners_lifecycle  # noqa: E402,F401
from . import api_runners_holds  # noqa: E402,F401
from . import api_runners_protocol  # noqa: E402,F401
# isort: on
