"""Addresses: every tenant page lives under `/w/<ws>/`, on this deployment's
public base URL. There is one address per artifact and no flat fallback."""
from __future__ import annotations

from django.conf import settings

from .models import WorkspaceAccessRequest, WorkspaceInvite


def base_url() -> str:
    return settings.CANOPY_PUBLIC_BASE_URL.rstrip("/")


class Unhomed(ValueError):
    """A row with no workspace has no address: there is no flat fallback."""


def scoped_path(workspace, path: str) -> str:
    """`path` under its workspace: `/w/<ws><path>`, app-relative.

    Every page that shows tenant data lives under `/w/:workspace/` — the share
    links too, and the bytes behind a walkthrough. There is ONE address per
    artifact and no flat form to fall back to (owner decision, 2026-10-08;
    canopy-web#1337): a flat link is a 404, so building one is a bug. A row with
    no workspace raises `Unhomed`; a caller that may hold one asks
    `scoped_path_or_none`."""
    slug = getattr(workspace, "slug", workspace)
    if not slug:
        raise Unhomed(f"no workspace to address {path!r} under")
    return f"/w/{slug}{path}"


def scoped_path_or_none(workspace, path: str) -> str | None:
    """`scoped_path`, or None for a row with no workspace (it has no address)."""
    try:
        return scoped_path(workspace, path)
    except Unhomed:
        return None


def scoped_url(workspace, path: str) -> str:
    """Absolute `scoped_path`, on this deployment's public base URL.

    Never `request.build_absolute_uri`: an MCP tool call reaches the route
    in-process with no real Host, so that minted `https://localhost/…` links
    (canopy-web#1289). CANOPY_PUBLIC_BASE_URL is the address people visit."""
    return f"{base_url()}{scoped_path(workspace, path)}"


def scoped_url_or_none(workspace, path: str) -> str | None:
    rel = scoped_path_or_none(workspace, path)
    return f"{base_url()}{rel}" if rel else None


def public_url(path: str) -> str:
    """Absolute URL of a page that is deliberately NOT under a workspace (an
    invite, a storyboard) — named so a flat link is never built by accident."""
    return f"{base_url()}{path}"


def access_request_path(req: WorkspaceAccessRequest) -> str:
    """The request's approve page, app-relative (push payloads use this)."""
    return f"/w/{req.workspace_id}/settings/access-requests/{req.pk}"


def access_request_link(req: WorkspaceAccessRequest) -> str:
    """Absolute deep link to the request's approve page. CANOPY_PUBLIC_BASE_URL
    carries the deployment prefix (/canopy on labs); the SPA sends a signed-out
    admin through login and back here (`next=`)."""
    return f"{base_url()}{access_request_path(req)}"


def invite_link(invite: WorkspaceInvite) -> str:
    """Absolute accept URL. CANOPY_PUBLIC_BASE_URL already carries the
    deployment's path prefix (/canopy on labs)."""
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}/invite/{invite.token}"
