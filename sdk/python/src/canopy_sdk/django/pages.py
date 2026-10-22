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


def page_token(request, *, agent: str = "", runner_requirements=()) -> str:
    """This page's token for this user, or "" for an unregistered route that
    chose no agent — and always "" in key mode, where the browser names its page
    instead. ``agent`` / ``runner_requirements`` are signed into it."""
    if conf.page_mode() != conf.SIGNED:
        return ""
    match = getattr(request, "resolver_match", None)
    view_name = getattr(match, "view_name", "") or ""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    return conf.page_tokens().issue(view_name, user.pk, agent=agent,
                                    runner_requirements=runner_requirements)


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


#: The ``PANEL`` keys a render may set, and the widget option each becomes.
PANEL_KEYS = {
    "mode": "mode", "launcher_label": "launcherLabel", "theme": "theme", "width": "width",
    "open": "open", "target": "target", "expandable": "expandable",
    "initial_display": "initialDisplay",
}


def panel_context(request=None, *, resource: str = "", backing_tool: str = "", visible_ids=(),
                  filters=None, path: str = "", agent: str = "", runner_requirements=(),
                  panel: dict | None = None) -> dict:
    """What ``canopy_host/panel.html`` needs, or a dict that renders nothing.

    ``visible_ids`` is the SELECTION the visitor can see — ids, never rows — and
    only what THIS visitor may see: "the agent sees what the user sees" is the
    access story, and keeping it true belongs to the page.

    ``agent`` chooses this page's agent in place of ``AGENT_SLUG``, and
    ``runner_requirements`` adds to the site's ``RUNNER_REQUIREMENTS`` (e.g.
    ``agent="labsdata", runner_requirements=["zdr"]`` on a page over real data).
    Both are signed into the page token, so the mint binds the visitor's token —
    and the grant this page's scopes buy — to that agent alone. Signed mode
    only: in key mode the browser names the page, so the choice lives in
    ``PAGE_AGENTS``.

    ``panel`` overrides ``PANEL`` for this render, key by key.
    """
    if (agent or runner_requirements) and conf.page_mode() != conf.SIGNED:
        raise ValueError("panel_context(agent=, runner_requirements=) needs PAGE_MODE 'signed'; "
                         "in key mode set CANOPY_HOST['PAGE_AGENTS'] for the page instead")
    unknown = set(panel or {}) - set(PANEL_KEYS)
    if unknown:
        raise ValueError(f"panel_context(panel=) has unknown keys: {sorted(unknown)}")
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
    options = {**conf.panel_options(), **(panel or {})}
    cfg = conf.raw()
    token = (page_token(request, agent=agent, runner_requirements=runner_requirements)
             if request is not None else "")
    ctx = {
        "ready": True,
        "base_url": str(cfg.get("CANOPY_BASE_URL", "")).rstrip("/"),
        "app_name": cfg.get("APP_NAME", ""),
        # The widget preselects it; the page token is what binds it.
        "agent": agent or conf.agent_slug(),
        "page_state": state,
        "page_token": token,
        "token_url": _token_url(token),
        # Key mode (an SPA shell rendering the panel): the widget names the page
        # on screen at each mint, as its path; PAGE_PATTERNS resolves it.
        "page_from_path": conf.page_mode() == conf.KEY,
        "mode": options.get("mode", "overlay"),
        "launcher_label": options.get("launcher_label", "Ask an agent"),
        "theme": options.get("theme") or {},
        "panel": {k: options[k] for k in ("width", "open", "target", "expandable",
                                          "initial_display") if k in options},
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
        # Only what the host set: an absent key keeps the widget's own default.
        **{PANEL_KEYS[k]: v for k, v in (ctx.get("panel") or {}).items()
           if v not in (None, "")},
        # For the page-side `canopyHost.updatePageState`, which trims to the same budget.
        "stateByteBudget": STATE_BYTE_BUDGET,
        "maxVisibleIds": MAX_VISIBLE_IDS,
    }
