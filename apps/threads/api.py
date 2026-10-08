"""/api/threads — bounded, moderated agent→agent conversations (models.py).

Mounted at /api/threads (and so /api/w/{ws}/threads). The thread row holds the
frame and its limits; each message is a tagged harness turn, read back through
the caller's visible turns and `turn_access` (services.py). The limits are
enforced where a message turn is created (guard.py), not here."""
from __future__ import annotations

import datetime as dt
import re

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.harness import provenance
from apps.harness.api import visible_turns_qs
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import services
from .models import MAX_PARTICIPANTS, MIN_PARTICIPANTS, AgentThread
from .schemas import ThreadCloseIn, ThreadIn, ThreadOut

router = Router(tags=["threads"])

#: A parent key the list may filter on: plain words, no `__` (it becomes an ORM
#: JSON key lookup, and a double underscore would be read as a further lookup).
_PARENT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")


def _my_workspaces(request: HttpRequest) -> set[str]:
    user = request.user
    return wsvc.user_workspace_slugs(user) if user.is_authenticated else set()


def _thread_or_404(request: HttpRequest, thread_id: str) -> AgentThread:
    """A thread in one of the caller's workspaces. Like huddles, a thread read spans
    every workspace the caller belongs to (its participants may live in several),
    never more; a non-member gets the same 404 as a missing thread."""
    t = AgentThread.objects.filter(id=thread_id, workspace_id__in=_my_workspaces(request)).first()
    if t is None:
        raise HttpError(404, f"thread {thread_id!r} not found")
    return t


def _agent_visible(request: HttpRequest, slug: str, *, pinned: bool) -> Agent:
    agent = Agent.objects.filter(slug=slug).first()
    ws = getattr(request, "workspace_slug", None) if pinned else None
    if (agent is None or not agent.workspace_id or (ws and agent.workspace_id != ws)
            or not wsvc.is_member(request.user, agent.workspace_id)):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _parent_is_thread_message() -> bool:
    """Depth limit 1 (v1): the turn the caller is running inside — its
    X-Canopy-Parent-Turn header, or a caller token's turn — is a thread message."""
    raw = provenance.requested_parent().get("turn")
    turn = provenance._turn(raw) if raw else None
    ref = turn.origin_ref if turn is not None and isinstance(turn.origin_ref, dict) else {}
    return ref.get("kind") == services.MESSAGE_KIND


@router.post("/", response={200: ThreadOut, 201: ThreadOut}, summary="Open an agent thread")
def open_thread(request: HttpRequest, payload: ThreadIn):
    """Open a bounded conversation between 2–6 named agents, moderated by one of the
    caller's agents (its workspace becomes the thread's; opening needs the editor
    role there). Each message is then a turn for the speaking agent tagged
    `origin_ref = {"kind": "thread_message", "thread": <id>, "n": <n>, "speaker": <slug>}`,
    refused at creation once the thread is closed, past `deadline_at` or out of
    `max_messages`. Re-opening — an OPEN thread with the same `parent` and the same
    set of agents — returns that thread with 200, so a moderator loop can resume.
    422 when called from inside a thread message (threads do not nest)."""
    agents = [p.agent for p in payload.participants]
    if not MIN_PARTICIPANTS <= len(agents) <= MAX_PARTICIPANTS:
        raise HttpError(422, f"a thread has {MIN_PARTICIPANTS} to {MAX_PARTICIPANTS} participants")
    if len(set(agents)) != len(agents):
        raise HttpError(422, "a thread's participants are distinct agents")
    if _parent_is_thread_message():
        raise HttpError(422, "a thread cannot be opened from inside a thread message")
    moderator = _agent_visible(request, payload.moderator, pinned=True)
    if not perms.can(request.user, moderator.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "opening a thread requires the editor role or above")
    for slug in agents:
        _agent_visible(request, slug, pinned=False)

    with transaction.atomic():
        for t in (AgentThread.objects.select_for_update()
                  .filter(workspace_id=moderator.workspace_id, status=AgentThread.OPEN,
                          moderator=moderator.slug, kind=payload.kind)):
            if (t.parent or {}) == (payload.parent or {}) and set(t.agents) == set(agents):
                return Status(200, services.out(t, used=services.messages_used(t.id)))
        now = timezone.now()
        t = AgentThread.objects.create(
            workspace_id=moderator.workspace_id, kind=payload.kind, purpose=payload.purpose,
            participants=[p.model_dump() for p in payload.participants],
            moderator=moderator.slug, parent=payload.parent or {}, context=payload.context,
            max_messages=payload.max_messages,
            deadline_at=now + dt.timedelta(minutes=payload.deadline_minutes),
            created_by=request.user if request.user.is_authenticated else None,
            created_at=now,
        )
    return Status(201, services.out(t, used=0))


@router.get("/", response=list[ThreadOut], summary="List agent threads")
def list_threads(request: HttpRequest, parent_key: str | None = None, parent_value: str | None = None,
                 agent: str | None = None, status: str | None = None, limit: int = 50):
    """Threads the caller can see, newest first (max 50), without their messages.
    `parent_key` + `parent_value` keep threads hanging off one thing (e.g.
    `parent_key=huddle&parent_value=<huddle id>`); `agent` keeps those it takes part
    in; `status` one status."""
    qs = AgentThread.objects.filter(workspace_id__in=_my_workspaces(request)).order_by("-created_at")
    if parent_key:
        if not _PARENT_KEY.match(parent_key):
            raise HttpError(422, f"bad parent_key {parent_key!r}")
        qs = qs.filter(**{f"parent__{parent_key}": parent_value or ""})
    if status:
        qs = qs.filter(status=status)
    limit = max(1, min(limit, 50))
    kept: list[AgentThread] = []
    for t in qs.iterator():
        if agent and agent not in t.agents:
            continue
        kept.append(t)
        if len(kept) >= limit:
            break
    used = services.used_counts([t.id for t in kept])
    return [services.out(t, used=used.get(t.id, 0)) for t in kept]


@router.get("/{thread_id}", response=ThreadOut, summary="Get one agent thread")
def get_thread(request: HttpRequest, thread_id: str):
    """One thread with its messages, in order: each message's turn status and, when
    you may read that turn's content, its prompt and the speaker's parsed reply
    block (from its close-out, else its transcript)."""
    t = _thread_or_404(request, thread_id)
    msgs = services.messages(t, user=request.user, visible_qs=visible_turns_qs(request, all_memberships=True))
    return services.out(t, used=services.messages_used(t.id), msgs=msgs)


@router.post("/{thread_id}/close", response=ThreadOut, summary="Close an agent thread")
def close_thread(request: HttpRequest, thread_id: str, payload: ThreadCloseIn):
    """The moderator ends the thread: `settled` (with an `outcome`, e.g.
    `{"result": "agreed", "proposal": {...}}`), `out_of_budget`, `timed_out` or
    `cancelled`. Only an open thread closes (409 otherwise); needs the editor role
    in the thread's workspace."""
    t = _thread_or_404(request, thread_id)
    if not perms.can(request.user, t.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "closing a thread requires the editor role or above")
    with transaction.atomic():
        t = AgentThread.objects.select_for_update().get(id=t.id)
        if t.status != AgentThread.OPEN:
            raise HttpError(409, f"thread {t.id} is {t.status}")
        t.status = payload.status
        t.outcome = payload.outcome or {}
        t.closed_at = timezone.now()
        t.save(update_fields=["status", "outcome", "closed_at"])
    msgs = services.messages(t, user=request.user, visible_qs=visible_turns_qs(request, all_memberships=True))
    return services.out(t, used=services.messages_used(t.id), msgs=msgs)
