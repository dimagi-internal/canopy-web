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
    """This page's token for this user, or "" for an unregistered route — and
    always "" in key mode, where the browser names its page instead."""
    if conf.page_mode() != conf.SIGNED:
        return ""
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


def panel_token_url() -> str:
    """Where the panel mints: ``PANEL_TOKEN_URL`` (a literal), else the host URL
    named by ``PANEL_TOKEN_URL_NAME``, else this app's ``canopy_host:panel_token``
    — reversed per request, so a host's ``FORCE_SCRIPT_NAME`` / prefix applies.
    ``""`` when none resolves."""
    cfg = conf.raw()
    literal = cfg.get("PANEL_TOKEN_URL") or ""
    if literal:
        return str(literal)
    for name in (cfg.get("PANEL_TOKEN_URL_NAME") or "", "canopy_host:panel_token"):
        if not name:
            continue
        try:
            return reverse(name)
        except NoReverseMatch:
            continue
    return ""


def _token_url(page: str) -> str:
    base = panel_token_url()
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
    ctx = {
        "ready": True,
        "base_url": str(cfg.get("CANOPY_BASE_URL", "")).rstrip("/"),
        "app_name": cfg.get("APP_NAME", ""),
        "agent": conf.agent_slug(),
        "page_state": state,
        "page_token": token,
        "token_url": _token_url(token),
        # Key mode (an SPA shell rendering the panel): the widget names the page
        # on screen at each mint, as its path; PAGE_PATTERNS resolves it.
        "page_from_path": conf.page_mode() == conf.KEY,
        "mode": options.get("mode", "overlay"),
        "launcher_label": options.get("launcher_label", "Ask an agent"),
        "theme": options.get("theme") or {},
    }
    ctx["options"] = panel_options(ctx)
    return ctx


def panel_options(ctx: dict) -> dict:
    """The widget's options as ONE JSON-serialisable dict — what the template
    renders through ``json_script``, so every value reaches the page literally."""
    return {
        "baseUrl": ctx.get("base_url", ""),
        "app": ctx.get("app_name", ""),
        "agent": ctx.get("agent", ""),
        "tokenUrl": ctx.get("token_url", ""),
        "pageFromPath": bool(ctx.get("page_from_path")),
        "mode": ctx.get("mode", "overlay"),
        "launcherLabel": ctx.get("launcher_label", "Ask an agent"),
        "theme": ctx.get("theme") or {},
        # For the page-side `canopyHost.updatePageState`, which trims to the same budget.
        "stateByteBudget": STATE_BYTE_BUDGET,
        "maxVisibleIds": MAX_VISIBLE_IDS,
    }
