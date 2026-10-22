"""/api/huddles — huddles as a DERIVED view over tagged turns and tasks.

No storage: see services.py. Mounted at /api/huddles (and so /api/w/{ws}/huddles)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.harness.api_turns import visible_turns_qs
from apps.realtime.groups import turn_workspace_slug
from apps.workspaces import services as wsvc

from . import services
from .schemas import HuddleOut, HuddleSummaryOut

router = Router(tags=["huddles"])


def _scope(request: HttpRequest):
    """(anchors' turns, summaries' turns, summaries' workspaces) for a huddle LIST.

    WHICH huddles are listed takes the read rule like every list
    (`wsvc.request_workspace_slugs`): a `/api/w/dimagi/huddles` read covers the
    caller's workspaces below dimagi (connect), a `/api/w/connect/…` read does not
    reach up to dimagi, and an unpinned read by a caller in more than one tree is
    refused (422) rather than mixing organisations.

    WHAT each listed huddle summarises — its rounds and the tasks it produced —
    spans the caller's memberships in the pin's tenant tree (`wsvc.tree_scope`),
    exactly as opening that huddle by id does (`get_huddle`): a huddle anchored in
    connect whose member lives in dimagi counts that member's outcome on the list
    too, rather than the list and the detail disagreeing. Never another tree's.
    Each turn still passes the same tenant/site filter, and each cell's content
    `turn_access`."""
    listed = wsvc.request_workspace_slugs(request)
    pin = getattr(request, "workspace_slug", None)
    workspaces = wsvc.tree_scope(request.user, pin) if pin else listed
    return (visible_turns_qs(request, slugs=listed),
            visible_turns_qs(request, slugs=workspaces), workspaces)


@router.get("/", response=list[HuddleSummaryOut], summary="List huddles")
def list_huddles(request: HttpRequest, agent: str | None = None, limit: int = 50):
    """Huddles the caller can see, newest first: one per leader-filed anchor turn, with
    its type, team, leader, members, how many rounds have been dispatched, whether it
    has been filed (`finished`) and how many board tasks it produced. `agent` keeps the
    huddles that agent led or was a member of."""
    listed, vis, workspaces = _scope(request)
    limit = max(1, min(limit, 200))
    kept, seen = [], set()
    # Anchors are one row per huddle, so walking them all to apply `agent` (a
    # membership test inside a JSON list) is cheap; summaries are batched after.
    for a in services.anchors(listed).order_by("-created_at"):
        ref = a.origin_ref if isinstance(a.origin_ref, dict) else {}
        hid = str(ref.get("huddle") or "")
        if not hid or hid in seen:
            continue
        leader = str(ref.get("leader") or (a.agent.slug if a.agent_id else ""))
        if agent and agent != leader and agent not in (ref.get("members") or []):
            continue
        seen.add(hid)
        kept.append(a)
        if len(kept) >= limit:
            break
    return services.summaries(kept, vis, workspaces)


@router.get("/{huddle_id}", response=HuddleOut, summary="Get one huddle")
def get_huddle(request: HttpRequest, huddle_id: str):
    """One huddle: a cell per (member, round) — the latest attempt's status and, when
    you may read that turn's content, its prompt and the member's parsed reply block
    (from its close-out, else its transcript) — plus the board tasks it produced, with
    their live status. 404 when no anchor turn you can see names this huddle."""
    # By id, so no pin is needed: find the anchor across every membership, then
    # read the huddle within ITS tenant tree — one organisation's huddle never
    # pulls in another's turns or tasks, whoever is asking.
    mine = wsvc.member_scope(request.user)
    anchor = (services.anchors(visible_turns_qs(request, slugs=mine))
              .filter(origin_ref__huddle=huddle_id).order_by("-created_at").first())
    if anchor is None:
        raise HttpError(404, f"huddle {huddle_id!r} not found")
    home = turn_workspace_slug(anchor)
    workspaces = wsvc.tree_scope(request.user, home) if home else set()
    vis = visible_turns_qs(request, slugs=workspaces)
    return services.detail(anchor, user=request.user, visible_qs=vis, workspaces=workspaces)
