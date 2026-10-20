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
from .schemas import AttentionOut, AttentionRowOut, HideIn, HideOut

router = Router(auth=session_auth, tags=["attention"])


@router.get("/", response=AttentionOut, summary="What is waiting on you")
def list_attention(request: HttpRequest) -> AttentionOut:
    """Everything waiting on the caller in scope — chats whose agent needs your
    next message, tasks asking you to decide or parked on you, requests to join a
    workspace you manage — as `now` (something happened in the last few days),
    `quiet` (nothing has for a while) and `hidden` (taken off your list by you;
    each returns when something new happens on it), plus `running` — not
    waiting on you, but yours and being worked right now. Newest first."""
    groups = services.attention(request, slugs=wsvc.request_workspace_slugs(request))
    return AttentionOut(**{k: [AttentionRowOut(**asdict(i)) for i in v] for k, v in groups.items()})


@router.post("/hide", response=HideOut, summary="Hide things from your list without deciding them")
def hide_attention(request: HttpRequest, payload: HideIn) -> HideOut:
    """Takes `keys` off your own list and nothing more: no task is closed,
    approved or declined, no chat is ended, nothing is sent, and the agent is not
    told. Each returns when something new happens on it, or after `days` when
    given."""
    return HideOut(count=services.hide(request.user, payload.keys, days=payload.days))


@router.delete("/hide/{key}", response=HideOut, summary="Put something you hid back on your list")
def unhide_attention(request: HttpRequest, key: str) -> HideOut:
    return HideOut(count=int(services.unhide(request.user, key)))
