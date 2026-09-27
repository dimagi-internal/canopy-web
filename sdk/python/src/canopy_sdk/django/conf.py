"""``CANOPY_HOST`` — the one setting the Django integration reads.

::

    CANOPY_HOST = {
        # The arrival (visitor assertion). Required for the panel.
        "SIGNING_KEY": env("CANOPY_SIGNING_KEY"),        # Ed25519 / P-256 PEM
        "CANOPY_BASE_URL": "https://labs.connect.dimagi.com/canopy",
        "APP_NAME": "connect-labs",                       # your Connected-site name
        "AGENT_SLUG": "ace",                              # which canopy tenant
        # The grant. All four present turns it on; any missing turns it off.
        "CLIENT_ID": "https://labs.connect.dimagi.com/canopy/oauth/client.json",
        "ISSUER": "https://labs.connect.dimagi.com",
        "RESOURCE": "https://labs.connect.dimagi.com/mcp/",
        "TOKEN_ENDPOINT": "https://labs.connect.dimagi.com/o/token/",
        # What each scope unlocks, and which pages grant which scopes.
        "SCOPE_TOOLS": {"marketplace:read": ["marketplace_orgs_get"]},
        "PAGE_SCOPES": {"marketplace:network": ["marketplace:read"]},
        # How a mint names its page (canopy_sdk.host.pages): "signed" (default)
        # — the server renders the page and signs its URL name; or "key" — an
        # SPA, whose browser names a PAGE_SCOPES key (or a path PAGE_PATTERNS
        # recognises). Key mode accepts only ":read" scopes unless listed in
        # WRITABLE_SCOPES.
        "PAGE_MODE": "signed",
        "PAGE_PATTERNS": {},        # key mode: {key: regex over location.pathname}
        "WRITABLE_SCOPES": [],
        # Optional hooks (dotted paths or callables):
        "SUBJECT_FOR_USER": None,   # user -> str; default str(user.pk)
        "SUBJECT_ACTIVE": None,     # sub -> bool; default: an active user with that pk
        "USER_CLAIMS": None,        # user -> {"name", "email", "email_verified"}
        "RETIRED_KEYS": [],
        "PAGE_TOKEN_MAX_AGE": 7200,
        # Where the panel mints. Default: reverse("canopy_host:panel_token").
        "PANEL_TOKEN_URL": "",       # a literal URL, or
        "PANEL_TOKEN_URL_NAME": "",  # a URL name of the host's own, reversed per request
        "PANEL": {"launcher_label": "Ask an agent", "mode": "overlay", "theme": {}},
    }

``CANOPY_HOST`` may also be a CALLABLE returning that dict (or a dotted path to
one), resolved on every read — for a host whose values derive from settings a
later settings module overrides (``PUBLIC_URL`` per environment) or that tests
override one at a time::

    def canopy_host():
        from django.conf import settings
        return {"SIGNING_KEY": settings.CANOPY_SIGNING_KEY,
                "ISSUER": settings.PUBLIC_URL, ...}

    CANOPY_HOST = canopy_host        # or "myapp.canopy.canopy_host"

Prefer that to a custom ``Mapping``: Django's debug page masks secrets only
inside a real ``dict`` (by key — ``SIGNING_KEY`` matches), and shows a callable
by name, never by value. What the SDK reads is always a fresh ``dict``.

Read on every call rather than cached at import, so ``override_settings`` in a
host's tests takes effect.
"""
from __future__ import annotations

from collections.abc import Mapping

from django.conf import settings
from django.utils.module_loading import import_string

from ..host.config import HostConfig, HostNotConfigured
from ..host.pages import KEY, MODES, SIGNED, PageRegistry, PageTokens


def raw() -> dict:
    """``CANOPY_HOST`` resolved now: a dict, a ``Mapping``, or a callable (or
    dotted path to one) returning either. Always a new ``dict``."""
    value = getattr(settings, "CANOPY_HOST", None)
    if isinstance(value, str) and value:
        value = import_string(value)
    if callable(value) and not isinstance(value, Mapping):
        value = value()
    return dict(value or {})


def _hook(name: str, default):
    value = raw().get(name)
    if not value:
        return default
    return import_string(value) if isinstance(value, str) else value


def is_configured() -> bool:
    cfg = raw()
    return bool(cfg.get("SIGNING_KEY") and cfg.get("CANOPY_BASE_URL") and cfg.get("APP_NAME"))


def get_host_config() -> HostConfig:
    cfg = raw()
    if not cfg.get("SIGNING_KEY"):
        raise HostNotConfigured("CANOPY_HOST['SIGNING_KEY'] is not set")
    return HostConfig(
        signing_key=cfg["SIGNING_KEY"],
        canopy_base_url=cfg.get("CANOPY_BASE_URL", ""),
        app_name=cfg.get("APP_NAME", ""),
        issuer=cfg.get("ISSUER", ""),
        resource=cfg.get("RESOURCE", ""),
        token_endpoint=cfg.get("TOKEN_ENDPOINT", ""),
        canopy_client_id=cfg.get("CLIENT_ID", ""),
        canopy_audience=cfg.get("CANOPY_AUDIENCE", ""),
        scope_tools=cfg.get("SCOPE_TOOLS") or {},
        retired_keys=tuple(cfg.get("RETIRED_KEYS") or ()),
    )


def page_mode() -> str:
    mode = str(raw().get("PAGE_MODE") or SIGNED)
    if mode not in MODES:
        raise ValueError(f"CANOPY_HOST['PAGE_MODE'] must be one of {MODES}, not {mode!r}")
    return mode


def page_tokens() -> PageTokens:
    """The SIGNED-mode registry (a server-rendered page's token)."""
    cfg = raw()
    return PageTokens(
        settings.SECRET_KEY, cfg.get("PAGE_SCOPES") or {},
        scope_tools=cfg.get("SCOPE_TOOLS") or {},
        max_age=int(cfg.get("PAGE_TOKEN_MAX_AGE") or 7200),
        salt="canopy_sdk.django.page",
        writable_scopes=cfg.get("WRITABLE_SCOPES") or (),
    )


def page_registry() -> PageRegistry:
    """The registry for ``PAGE_MODE``: ``PageTokens`` (signed) or ``PageRegistry`` (key)."""
    if page_mode() == KEY:
        cfg = raw()
        return PageRegistry(cfg.get("PAGE_SCOPES") or {}, scope_tools=cfg.get("SCOPE_TOOLS") or {},
                            patterns=cfg.get("PAGE_PATTERNS") or {},
                            writable_scopes=cfg.get("WRITABLE_SCOPES") or ())
    return page_tokens()


def page_scopes(value, user) -> tuple[str, ...]:
    """The scopes the page ``value`` (a signed page token, or a page key)
    grants ``user`` — ``()`` for anything else. The mint's one question."""
    user_id = getattr(user, "pk", None) if user is not None else None
    return page_registry().scopes_for(value, user_id)


def agent_slug() -> str:
    return str(raw().get("AGENT_SLUG") or "")


def panel_options() -> dict:
    return dict(raw().get("PANEL") or {})


def _default_subject_for_user(user) -> str:
    return str(user.pk)


def _default_subject_active(subject: str) -> bool:
    from django.contrib.auth import get_user_model

    try:
        return get_user_model().objects.filter(pk=subject, is_active=True).exists()
    except (TypeError, ValueError):
        return False


def _default_user_claims(user) -> dict:
    # `email_verified` defaults to False: vouching for an address is a decision
    # the host makes on purpose (USER_CLAIMS), never a default.
    name = ""
    for attr in ("get_display_name", "get_full_name"):
        fn = getattr(user, attr, None)
        if callable(fn):
            name = fn() or ""
            if name:
                break
    return {"name": name or user.get_username(), "email": getattr(user, "email", "") or "",
            "email_verified": False}


def subject_for_user(user) -> str:
    return str(_hook("SUBJECT_FOR_USER", _default_subject_for_user)(user))


def subject_active():
    return _hook("SUBJECT_ACTIVE", _default_subject_active)


def user_claims(user) -> dict:
    claims = dict(_hook("USER_CLAIMS", _default_user_claims)(user) or {})
    return {k: claims[k] for k in ("name", "email", "email_verified") if k in claims}
