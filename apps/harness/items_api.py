"""Django Ninja routers for Items — the supervisor's queue.

Kept out of api.py, which already owns the runner + turn lifecycle (and, since
#218, the schedule routes live in their own api_schedules.py for the same
reason). Two routers because the collection is agent-scoped (whose queue?) while
the resource is not (an item id is globally unique).

Authz mirrors apps/agents: an item is visible iff its agent is. A non-member gets
404, never 403 — no existence leak.

See docs/superpowers/specs/2026-07-15-item-and-turn-design.md.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.api import _get_agent_or_404, _visible_agent_workspace_ids
from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from . import services
from .models import Item
from .schemas import ItemDecideIn, ItemDismissIn, ItemIn, ItemOut

agent_items_router = Router(auth=session_auth, tags=["items"])
items_router = Router(auth=session_auth, tags=["items"])


def _payload(item: Item) -> dict:
    return {
        "id": item.id,
        "agent_slug": item.agent.slug,
        "idempotency_key": item.idempotency_key,
        "kind": item.kind,
        "title": item.title,
        "body": item.body,
        "origin": item.origin,
        "origin_ref": item.origin_ref,
        "state": item.state,
        "decision": item.decision,
        "comment": item.comment,
        "decided_by": item.decided_by,
        "decided_by_email": item.decided_by_user.email if item.decided_by_user_id else (item.decided_by or None),
        "decided_at": item.decided_at,
        "dispatch": item.dispatch,
        "dispatched_at": item.dispatched_at,
        "batch_key": item.batch_key,
        "created_at": item.created_at,
    }


def _item_or_404(request: HttpRequest, item_id: uuid.UUID) -> Item:
    """An item is reachable iff its agent's workspace is visible to the caller.

    Built from _visible_agent_workspace_ids — the single definition — so this can
    never drift from what the agents list shows, which is the failure that helper
    exists to prevent. Membership is tested in PYTHON, not as a queryset filter:
    the set may contain None (the unhomed-agent case) and SQL `IN` never matches
    NULL.
    """
    item = Item.objects.filter(pk=item_id).select_related("agent").first()
    if item is None or item.agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, "item not found")
    return item


@agent_items_router.get("/{slug}/items/", response=list[ItemOut], summary="List an agent's items",
                        openapi_extra={"x-mcp-expose": True})
def list_items(
    request: HttpRequest, slug: str, state: str = "", kind: str = "", batch: str = "",
) -> list[dict]:
    agent = _get_agent_or_404(request, slug)
    qs = agent.items.select_related("agent")
    if state:
        qs = qs.filter(state=state)
    if kind:
        qs = qs.filter(kind=kind)
    if batch:
        qs = qs.filter(batch_key=batch)
    return [_payload(i) for i in qs]


@agent_items_router.post("/{slug}/items/", response={201: list[ItemOut]},
                         summary="Raise items for an agent (batch, idempotent)",
                         openapi_extra={"x-mcp-expose": True})
def create_items(request: HttpRequest, slug: str, payload: list[ItemIn]):
    agent = _get_agent_or_404(request, slug)
    items = services.create_items(
        agent=agent, payloads=[p.dict() for p in payload],
    )
    return 201, [_payload(i) for i in items]


# Rank order for the inbox: a decision to make (review) outranks a question the
# agent is blocked on. Mirrors the frontend band order.
_KIND_RANK = {Item.REVIEW: 0, Item.QUESTION: 1}


@items_router.get("/", response=list[ItemOut],
                  summary="Fleet inbox — items across every agent you can see")
def list_fleet_items(request: HttpRequest, state: str = Item.OPEN, kind: str = "") -> list[dict]:
    """The supervisor's home screen, as a pure query: open items across the
    caller's visible agents, ranked review -> question then oldest-first. Replaces
    the old needs_you aggregation. Defaults to state=open (the inbox); pass an
    explicit state to widen. Authz reuses the single agent-visibility predicate, so
    it can never show an item whose agent the agents list would hide."""
    visible = _visible_agent_workspace_ids(request)
    qs = Item.objects.filter(agent__workspace_id__in=visible).select_related("agent")
    if state:
        qs = qs.filter(state=state)
    if kind:
        qs = qs.filter(kind=kind)
    rows = sorted(qs, key=lambda i: (_KIND_RANK.get(i.kind, 9), i.created_at))
    return [_payload(i) for i in rows]


@items_router.get("/{item_id}/", response=ItemOut, summary="Get an item")
def get_item(request: HttpRequest, item_id: uuid.UUID) -> dict:
    return _payload(_item_or_404(request, item_id))


@items_router.post("/{item_id}/decide", response=ItemOut,
                   summary="Decide an item (implement dispatches its work)")
def decide_item(request: HttpRequest, item_id: uuid.UUID, payload: ItemDecideIn) -> dict:
    item = _item_or_404(request, item_id)
    try:
        item, _turns = services.decide_item(
            item, decision=payload.decision, comment=payload.comment,
            by=request.user.email or request.user.get_username(),
            actor_workspace_slugs=wsvc.request_workspace_slugs(request),
            decided_by_user=request.user,
        )
    except services.AlreadyDecidedError as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        # A bad dispatch spec, or a question with no answer. The decision rolled
        # back (services.decide_item is atomic) — the item is still open.
        raise HttpError(422, str(exc)) from exc
    return _payload(item)


@items_router.post("/{item_id}/dismiss", response=ItemOut, summary="Dismiss an item")
def dismiss_item(request: HttpRequest, item_id: uuid.UUID,
                 payload: ItemDismissIn | None = None) -> dict:
    item = _item_or_404(request, item_id)
    try:
        item = services.dismiss_item(
            item, by=request.user.email or request.user.get_username(),
            decided_by_user=request.user,
            comment=(payload.comment if payload else ""),
        )
    except services.AlreadyDecidedError as exc:
        # Already decided or dismissed — mirror decide's 409 so a double-click or a
        # dismiss-after-approve can't overwrite the decision record.
        raise HttpError(409, str(exc)) from exc
    return _payload(item)
