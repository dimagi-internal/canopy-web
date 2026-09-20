"""Django Ninja routers for the ask queue — the supervisor's inbox.

**The rows are TASKS now.** An `Item` used to be its own model; a task carries
the ask instead (`AgentTask.ask_kind` / `ask_body` / `decision` …). The paths and
the response shape are unchanged on purpose: Ada stores item ids, the phone and
the agent workspace fetch these routes, and a rename would break all of them for
no gain. `id` is the task's `uuid`, which a migrated item kept, so every id that
ever worked still resolves.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents import services as agent_services
from apps.agents.api import _get_agent_or_404, _visible_agent_workspace_ids
from apps.agents.models import AgentTask
from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from .schemas import ItemDecideIn, ItemDismissIn, ItemIn, ItemOut

agent_items_router = Router(auth=session_auth, tags=["items"])
items_router = Router(auth=session_auth, tags=["items"])


def _payload(task: AgentTask) -> dict:
    return {
        "id": task.uuid,
        "agent_slug": task.agent.slug,
        "idempotency_key": task.idempotency_key or "",
        "kind": task.ask_kind,
        "title": task.title,
        "body": task.ask_body,
        "origin": task.origin,
        "origin_ref": task.origin_ref,
        "state": task.ask_state,
        "decision": task.decision,
        "comment": task.comment,
        "decided_by": task.decided_by,
        "decided_by_email": (task.decided_by_user.email if task.decided_by_user_id
                             else (task.decided_by or None)),
        "decided_at": task.decided_at,
        "dispatch": task.dispatch,
        "dispatched_at": task.dispatched_at,
        "batch_key": task.batch_key,
        "created_at": task.created_at,
    }


def _ask_payload(p: dict) -> dict:
    """An `ItemIn` body in the task vocabulary. `kind`/`body` were the ask's own
    words before the ask moved onto the task; the wire keeps the old names."""
    p = dict(p)
    p["ask_kind"] = p.pop("kind", "") or AgentTask.ASK_REVIEW
    p["ask_body"] = p.pop("body", "") or ""
    return p


def _asks(qs):
    """Only tasks that ask a human something. A task with no ask is ordinary
    work in flight and has never belonged in this queue."""
    return qs.exclude(ask_kind="")


def _by_state(qs, state: str):
    """`open` · `decided` · `dismissed`, as a query rather than in Python, so a
    fleet-wide list does not have to load every task to filter it."""
    if state == "open":
        return qs.filter(decided_at__isnull=True)
    if state == "decided":
        return qs.filter(decided_at__isnull=False, ask_dismissed=False)
    if state == "dismissed":
        return qs.filter(decided_at__isnull=False, ask_dismissed=True)
    return qs


def _task_or_404(request: HttpRequest, item_id: uuid.UUID) -> AgentTask:
    """Reachable iff its agent's workspace is visible to the caller.

    Built from `_visible_agent_workspace_ids` — the single definition — so this
    cannot drift from what the agents list shows. Membership is tested in
    PYTHON, not as a queryset filter: the set may contain None (the unhomed-agent
    case) and SQL `IN` never matches NULL.
    """
    task = AgentTask.objects.filter(uuid=item_id).select_related("agent").first()
    if task is None or task.agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, "item not found")
    return task


@agent_items_router.get("/{slug}/items/", response=list[ItemOut],
                        summary="List an agent's items",)
def list_items(
    request: HttpRequest, slug: str, state: str = "", kind: str = "", batch: str = "",
) -> list[dict]:
    agent = _get_agent_or_404(request, slug)
    qs = _asks(agent.tasks.select_related("agent"))
    if state:
        qs = _by_state(qs, state)
    if kind:
        qs = qs.filter(ask_kind=kind)
    if batch:
        qs = qs.filter(batch_key=batch)
    return [_payload(t) for t in qs]


@agent_items_router.post("/{slug}/items/", response={201: list[ItemOut]},
                         summary="Raise items for an agent (batch, idempotent)",)
def create_items(request: HttpRequest, slug: str, payload: list[ItemIn]):
    agent = _get_agent_or_404(request, slug)
    # `.dict()` first: `dispatch` holds nested TurnSpecIn models, and a JSON
    # column cannot store those — it raised a 500 until this was a plain dict.
    tasks = agent_services.raise_asks(
        agent=agent,
        payloads=[_ask_payload(p.dict()) for p in payload],
    )
    return 201, [_payload(t) for t in tasks]


# Rank order for the inbox: a decision to make (review) outranks a question the
# agent is blocked on. Mirrors the frontend band order.
_KIND_RANK = {AgentTask.ASK_REVIEW: 0, AgentTask.ASK_QUESTION: 1}


@items_router.get("/", response=list[ItemOut],
                  summary="Fleet inbox — items across every agent you can see")
def list_fleet_items(request: HttpRequest, state: str = "open", kind: str = "") -> list[dict]:
    """The supervisor's home screen, as a pure query: open asks across the
    caller's visible agents, ranked review -> question then oldest-first.
    Defaults to state=open (the inbox); pass an explicit state to widen. Authz
    reuses the single agent-visibility predicate, so it can never show an ask
    whose agent the agents list would hide."""
    visible = _visible_agent_workspace_ids(request)
    qs = _asks(AgentTask.objects.filter(agent__workspace_id__in=visible)
               .select_related("agent"))
    if state:
        qs = _by_state(qs, state)
    if kind:
        qs = qs.filter(ask_kind=kind)
    rows = sorted(qs, key=lambda t: (_KIND_RANK.get(t.ask_kind, 9), t.created_at))
    return [_payload(t) for t in rows]


@items_router.get("/{item_id}/", response=ItemOut, summary="Get an item")
def get_item(request: HttpRequest, item_id: uuid.UUID) -> dict:
    return _payload(_task_or_404(request, item_id))


@items_router.post("/{item_id}/decide", response=ItemOut,
                   summary="Decide an item (implement dispatches its work)")
def decide_item(request: HttpRequest, item_id: uuid.UUID, payload: ItemDecideIn) -> dict:
    task = _task_or_404(request, item_id)
    try:
        task, _turns = agent_services.decide_ask(
            task, decision=payload.decision, comment=payload.comment,
            by=request.user.email or request.user.get_username(),
            actor_workspace_slugs=wsvc.request_workspace_slugs(request),
            decided_by_user=request.user,
        )
    except agent_services.AlreadyDecidedError as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        # A bad dispatch spec, or a question with no answer. The decision rolled
        # back (decide_ask is atomic) — the ask is still open.
        raise HttpError(422, str(exc)) from exc
    return _payload(task)


@items_router.post("/{item_id}/dismiss", response=ItemOut, summary="Dismiss an item")
def dismiss_item(request: HttpRequest, item_id: uuid.UUID,
                 payload: ItemDismissIn | None = None) -> dict:
    task = _task_or_404(request, item_id)
    try:
        task = agent_services.dismiss_ask(
            task, by=request.user.email or request.user.get_username(),
            decided_by_user=request.user,
            comment=(payload.comment if payload else ""),
        )
    except agent_services.AlreadyDecidedError as exc:
        # Already decided or dismissed — mirror decide's 409 so a double-click or
        # a dismiss-after-approve cannot overwrite the decision record.
        raise HttpError(409, str(exc)) from exc
    return _payload(task)
