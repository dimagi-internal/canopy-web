"""/api/huddles — huddles as a DERIVED view over tagged turns and tasks.

No storage: see services.py. Mounted at /api/huddles (and so /api/w/{ws}/huddles)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.harness.api import visible_turns_qs
from apps.workspaces import services as wsvc

from . import services
from .schemas import HuddleOut, HuddleSummaryOut

router = Router(tags=["huddles"])


def _scope(request: HttpRequest):
    """(visible turns, workspaces) for a huddle read. A huddle's members can live
    in several workspaces, so a `/api/w/{ws}/huddles` read spans EVERY workspace
    the caller belongs to rather than the pinned one — never more: each turn still
    passes the same tenant/site filter, and each cell's content `turn_access`."""
    user = request.user
    workspaces = wsvc.user_workspace_slugs(user) if user.is_authenticated else set()
    return visible_turns_qs(request, all_memberships=True), workspaces


@router.get("/", response=list[HuddleSummaryOut], summary="List huddles")
def list_huddles(request: HttpRequest, agent: str | None = None, limit: int = 50):
    """Huddles the caller can see, newest first: one per leader-filed anchor turn, with
    its type, team, leader, members, how many rounds have been dispatched, whether it
    has been filed (`finished`) and how many board tasks it produced. `agent` keeps the
    huddles that agent led or was a member of."""
    vis, workspaces = _scope(request)
    limit = max(1, min(limit, 200))
    kept, seen = [], set()
    # Anchors are one row per huddle, so walking them all to apply `agent` (a
    # membership test inside a JSON list) is cheap; summaries are batched after.
    for a in services.anchors(vis).order_by("-created_at"):
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
    vis, workspaces = _scope(request)
    anchor = services.anchors(vis).filter(origin_ref__huddle=huddle_id).order_by("-created_at").first()
    if anchor is None:
        raise HttpError(404, f"huddle {huddle_id!r} not found")
    return services.detail(anchor, user=request.user, visible_qs=vis, workspaces=workspaces)
