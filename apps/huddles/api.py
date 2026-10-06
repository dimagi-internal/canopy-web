"""/api/huddles — huddles as a DERIVED view over tagged turns and tasks.

No storage: see services.py. Mounted at /api/huddles (and so /api/w/{ws}/huddles)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.harness.api import visible_turns_qs

from . import services
from .schemas import HuddleOut, HuddleSummaryOut

router = Router(tags=["huddles"])


@router.get("/", response=list[HuddleSummaryOut], summary="List huddles")
def list_huddles(request: HttpRequest, agent: str | None = None, limit: int = 50):
    """Huddles the caller can see, newest first: one per leader-filed anchor turn, with
    its type, team, leader, members, how many rounds have been dispatched, whether it
    has been filed (`finished`) and how many board tasks it produced. `agent` keeps the
    huddles that agent led or was a member of."""
    vis = visible_turns_qs(request)
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
    return services.summaries(kept, vis)


@router.get("/{huddle_id}", response=HuddleOut, summary="Get one huddle")
def get_huddle(request: HttpRequest, huddle_id: str):
    """One huddle: a cell per (member, round) — the latest attempt's status and, when
    you may read that turn's content, its prompt and the member's parsed reply block
    (from its close-out, else its transcript) — plus the board tasks it produced, with
    their live status. 404 when no anchor turn you can see names this huddle."""
    vis = visible_turns_qs(request)
    anchor = services.anchors(vis).filter(origin_ref__huddle=huddle_id).order_by("-created_at").first()
    if anchor is None:
        raise HttpError(404, f"huddle {huddle_id!r} not found")
    return services.detail(anchor, user=request.user, visible_qs=vis)
