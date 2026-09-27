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
        # Optional hooks (dotted paths or callables):
        "SUBJECT_FOR_USER": None,   # user -> str; default str(user.pk)
        "SUBJECT_ACTIVE": None,     # sub -> bool; default: an active user with that pk
        "USER_CLAIMS": None,        # user -> {"name", "email", "email_verified"}
        "RETIRED_KEYS": [],
        "PAGE_TOKEN_MAX_AGE": 7200,
        "PANEL": {"launcher_label": "Ask an agent", "mode": "overlay", "theme": {}},
    }

Read on every call rather than cached at import, so ``override_settings`` in a
host's tests takes effect.
"""
from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from ..host.config import HostConfig, HostNotConfigured
from ..host.pages import PageTokens


def raw() -> dict:
    return dict(getattr(settings, "CANOPY_HOST", None) or {})


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


def page_tokens() -> PageTokens:
    cfg = raw()
    return PageTokens(
        settings.SECRET_KEY, cfg.get("PAGE_SCOPES") or {},
        scope_tools=cfg.get("SCOPE_TOOLS") or {},
        max_age=int(cfg.get("PAGE_TOKEN_MAX_AGE") or 7200),
        salt="canopy_sdk.django.page",
    )


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
