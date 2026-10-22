"""Schemas for the embedded widget's routes (`/api/embed/…`)."""
from __future__ import annotations

from apps.common.schemas import StrictModel
from apps.contacts.people_schemas import SessionMemoryOut


class EmbedAgentOut(StrictModel):
    """One agent an embedding app may offer the caller — `GET /api/embed/agents`.

    Deliberately thin: what a picker needs to render a choice, and nothing that
    describes how the agent RUNS. An embedded widget is the least trusted
    surface canopy has, so runtime detail (repo, engine, secret-reference names,
    runner assignments) stays on the owner-only agent routes.

    `workspace` is included because it is the one non-cosmetic field — it tells
    the host which tenant a session started with this agent will belong to.
    """

    slug: str
    name: str
    description: str
    avatar_url: str
    workspace: str


class EmbedSelfOut(StrictModel):
    """`GET /api/embed/self` — whether canopy offers the widget on its own pages.

    Its own endpoint rather than a field on `/api/me/` because it is a
    deployment setting, not a property of the user, and the frontend reads it
    once to decide whether to mount anything at all.
    """

    enabled: bool
    app: str
    #: Optional preselected agent; empty means show the picker.
    agent: str


class EmbedSelfTokenOut(StrictModel):
    """`POST /api/embed/token` — a delegated token for the caller.

    Same shape a third-party host's own token endpoint returns to its page, so
    the frontend code path is identical whether the host is canopy or not.
    """

    token: str
    expires_at: str
    #: Whether canopy also holds a grant to act as you at its OWN tools for
    #: the page you named (`?page=`) — see `apps/host_grants/self_host.py`. False
    #: for an unregistered page, or when canopy-web is not set up as a host.
    host_grant: bool = False


class EmbedSessionMemoryOut(SessionMemoryOut):
    """`GET /api/embed/sessions/{id}/agent-memory` — agent memory in this session
    and what you granted its agent, as the widget may SHOW it. Read-only: a site
    acting for you never changes it or grants anything; `manage_path` is where you
    do that yourself, in canopy."""

    #: canopy's own chat page for this session, relative to canopy's base.
    manage_path: str
