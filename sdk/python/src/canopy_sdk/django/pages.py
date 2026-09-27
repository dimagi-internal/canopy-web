"""The page token and the panel's template context."""
from __future__ import annotations

import json

from django.urls import NoReverseMatch, reverse

from . import conf

#: canopy caps a page state at 8 KiB of BYTES; a count cap does not keep under
#: it (400 slugs serialise to ~11 KiB). Trimmed by serialised size, with
#: headroom for what canopy adds.
STATE_BYTE_BUDGET = 7 * 1024
MAX_VISIBLE_IDS = 400


def page_token(request) -> str:
    """This page's token for this user, or "" for an unregistered route."""
    match = getattr(request, "resolver_match", None)
    view_name = getattr(match, "view_name", "") or ""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    return conf.page_tokens().issue(view_name, user.pk)


def _fit_ids(state: dict) -> list[str]:
    """The longest PREFIX of ``visible_ids`` that keeps ``state`` inside the
    budget — "the first N of what I see" is the honest truncation."""
    ids = list(state.get("visible_ids") or [])
    size = len(json.dumps({**state, "visible_ids": []}).encode())
    kept = []
    for i in ids:
        size += len(json.dumps(i).encode()) + 2
        if size > STATE_BYTE_BUDGET:
            break
        kept.append(i)
    return kept


def _token_url(page: str) -> str:
    base = conf.raw().get("PANEL_TOKEN_URL") or ""
    if not base:
        try:
            base = reverse("canopy_host:panel_token")
        except NoReverseMatch:
            base = ""
    if base and page:
        from urllib.parse import quote

        base += ("&" if "?" in base else "?") + "page=" + quote(page, safe="")
    return base


def panel_context(request=None, *, resource: str = "", backing_tool: str = "", visible_ids=(),
                  filters=None, path: str = "") -> dict:
    """What ``canopy_host/panel.html`` needs, or a dict that renders nothing.

    ``visible_ids`` is the SELECTION the visitor can see — ids, never rows — and
    only what THIS visitor may see: "the agent sees what the user sees" is the
    access story, and keeping it true belongs to the page.
    """
    ready = conf.is_configured()
    state = None
    if resource:
        state = {"resource": resource,
                 "visible_ids": [str(i) for i in visible_ids][:MAX_VISIBLE_IDS]}
        if backing_tool:
            state["backing_tool"] = backing_tool
        if filters:
            state["filters"] = filters
        if path:
            state["path"] = path
        state["visible_ids"] = _fit_ids(state)
    if not ready:
        return {"ready": False, "page_state": state}
    options = conf.panel_options()
    cfg = conf.raw()
    token = page_token(request) if request is not None else ""
    return {
        "ready": True,
        "base_url": str(cfg.get("CANOPY_BASE_URL", "")).rstrip("/"),
        "app_name": cfg.get("APP_NAME", ""),
        "agent": conf.agent_slug(),
        "page_state": state,
        "page_token": token,
        "token_url": _token_url(token),
        "mode": options.get("mode", "overlay"),
        "launcher_label": options.get("launcher_label", "Ask an agent"),
        "theme": options.get("theme") or {},
    }
