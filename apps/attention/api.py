"""`/api/attention/` — what is waiting on you, in one list.

The same rule everywhere a person is asked for something: Home reads it, MCP
serves it as a tool, any client can. Scope is the read rule (a pinned
`/api/w/<ws>/attention/` is that workspace and the ones below it you are in);
a page spanning organisations reads once per organisation and labels each row.
"""
from __future__ import annotations

from dataclasses import asdict

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from . import services
from .schemas import AttentionOut, AttentionRowOut, LetGoIn, LetGoOut

router = Router(auth=session_auth, tags=["attention"])


@router.get("/", response=AttentionOut, summary="What is waiting on you")
def list_attention(request: HttpRequest) -> AttentionOut:
    """Everything waiting on the caller in scope — chats whose agent needs your
    next message, tasks asking you to decide or parked on you, requests to join a
    workspace you manage — as `now` (something happened in the last few days),
    `quiet` (nothing has for a while) and `let_go` (set aside by you; each returns
    when something new happens on it). Newest first."""
    groups = services.attention(request, slugs=wsvc.request_workspace_slugs(request))
    return AttentionOut(**{k: [AttentionRowOut(**asdict(i)) for i in v] for k, v in groups.items()})


@router.post("/let-go", response=LetGoOut, summary="Set things aside without deciding them")
def let_go(request: HttpRequest, payload: LetGoIn) -> LetGoOut:
    """Takes `keys` off your list without answering them: nothing is approved,
    declined or sent, and the agent is not told. Each returns when something new
    happens on it, or after `days` when given. Touches only your own list."""
    return LetGoOut(count=services.let_go(request.user, payload.keys, days=payload.days))


@router.delete("/let-go/{key}", response=LetGoOut, summary="Bring something you let go back")
def take_back(request: HttpRequest, key: str) -> LetGoOut:
    return LetGoOut(count=int(services.take_back(request.user, key)))
