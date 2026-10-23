"""``current()`` — canopy's site agents for this Django host, cached, failing closed.

::

    from canopy_sdk.django import site_agents

    current = site_agents.current()
    if current is None:
        ...                       # show no agent: canopy has said nothing for a day
    elif current.stale:
        ...                       # canopy unreachable; this is its last word, current.age seconds old
    agent = current.agent("labsdata") if current else None

    error = site_agents.last_error()    # why the last refresh failed, or None
    if error:
        ...                       # error.code: refused, unknown_issuer, ambiguous_issuer,
                                  # workspace_mismatch, not_configured, unreachable…

    site_agents.refresh()         # a host view's "re-read now": raises SiteAgentsError

Read through the host's Django cache (use a shared one — Redis, the database —
so every worker sees one copy):

* a good copy is reused for ``FRESH_SECONDS`` (5 minutes), then refreshed on
  the next read;
* a failed refresh serves the last good copy marked ``stale=True`` — for up to
  ``STALE_SECONDS`` (24 hours) after it was fetched — and is not retried for
  ``RETRY_SECONDS``, so an outage at canopy is not a request per page view;
* after that, ``None``. A host treats None as "show no agent".

It asks with ``CANOPY_HOST``: ``SIGNING_KEY``, ``APP_NAME``,
``CANOPY_BASE_URL``, ``CLIENT_ID`` (canopy's identity, when it is not its
address) and ``WORKSPACE`` (which tenant's registration of the name) — or,
without it, ``AGENT_SLUG``, which names the tenant through that agent and
stops working if the agent is deleted or moved. The cache is per tenant
selector, so changing either takes effect on the next read.

``last_error()`` is the last refresh's failure (``SiteAgentsError`` with its
``code`` and a message safe to show), kept in the same cache and cleared by
the next success — so a page that shows no agent can say why instead of
guessing "unreachable".
"""
from __future__ import annotations

import hashlib
import logging
import time

from ..host.client_keys import ClientKeyResolver
from ..host.config import HostNotConfigured
from ..site_agents import NOT_CONFIGURED, SiteAgents, SiteAgentsError, fetch_site_agents
from . import conf

log = logging.getLogger("canopy_sdk.django")

FRESH_SECONDS = 300
STALE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60


def _selector(config) -> tuple[str, str]:
    """``(workspace, agent)`` this host names its tenant by. With ``WORKSPACE``
    set the agent is not sent: in this request its only job would be naming the
    tenant, and a deleted or moved agent would then break a listing the
    workspace alone answers."""
    workspace = config.workspace
    return workspace, "" if workspace else conf.agent_slug()


def _keys(config) -> tuple[str, str, str]:
    workspace, agent = _selector(config)
    scope = hashlib.sha256(f"{config.canopy_base_url}|{config.app_name}|w={workspace}|a={agent}"
                           .encode()).hexdigest()[:16]
    return (f"canopy_sdk:site_agents:{scope}:fresh", f"canopy_sdk:site_agents:{scope}:last_good",
            f"canopy_sdk:site_agents:{scope}:failed")


def _error_key(config) -> str:
    return _keys(config)[0].rsplit(":", 1)[0] + ":error"


def _record(config, error: SiteAgentsError | None, now: float) -> None:
    from django.core.cache import cache

    if error is None:
        cache.delete(_error_key(config))
    else:
        cache.set(_error_key(config), {**error.as_dict(), "at": now}, STALE_SECONDS + FRESH_SECONDS)


def refresh(*, now: float | None = None, get=None) -> SiteAgents:
    """Fetch and verify the site agents now, and cache it — what a host view calls
    to force a re-read (it ignores the fresh copy and the retry pause). Raises
    ``SiteAgentsError`` (``not_configured`` for a host missing a setting); the
    failure is also what ``last_error()`` returns until a refresh succeeds."""
    from django.core.cache import cache

    t = time.time() if now is None else now
    try:
        config = conf.get_host_config()
    except HostNotConfigured as exc:
        raise SiteAgentsError(NOT_CONFIGURED, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a malformed key or setting
        raise SiteAgentsError(NOT_CONFIGURED, f"CANOPY_HOST is not usable: {type(exc).__name__}") from exc
    fresh, last_good, failed = _keys(config)
    try:
        if not config.assertions_enabled:
            raise SiteAgentsError(NOT_CONFIGURED, "CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME "
                                                  "to ask for its agents")
        workspace, agent = _selector(config)
        listing = fetch_site_agents(config.canopy_base_url, config.app_name, config.signing_key,
                                    ClientKeyResolver(cache=cache),
                                    client_id=config.canopy_client_id or None, agent=agent,
                                    workspace=workspace, get=get)
    except SiteAgentsError as exc:
        _record(config, exc, t)
        raise
    except Exception as exc:
        _record(config, SiteAgentsError("error", f"the site agents could not be read: "
                                                 f"{type(exc).__name__}"), t)
        raise
    entry = {"claims": listing.claims, "fetched_at": t}
    cache.set(fresh, entry, FRESH_SECONDS)
    cache.set(last_good, entry, STALE_SECONDS + FRESH_SECONDS)
    cache.delete(failed)
    _record(config, None, t)
    return listing


def last_error() -> SiteAgentsError | None:
    """Why the last refresh failed — ``code`` (``not_configured``, ``unreachable``,
    ``refused``, ``unknown_issuer``, ``ambiguous_issuer``, ``workspace_mismatch``,
    ``bad_signature``…) and a message safe to show an operator — or None after a
    success. A host that cannot be configured gets ``not_configured`` at once.
    Never raises."""
    from django.core.cache import cache

    try:
        config = conf.get_host_config()
    except Exception as exc:  # noqa: BLE001 - HostNotConfigured or a malformed key
        return SiteAgentsError(NOT_CONFIGURED, str(exc) if isinstance(exc, HostNotConfigured)
                               else f"CANOPY_HOST is not usable: {type(exc).__name__}")
    if not config.assertions_enabled:
        return SiteAgentsError(NOT_CONFIGURED, "CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME "
                                               "to ask for its agents")
    try:
        return SiteAgentsError.from_dict(cache.get(_error_key(config)))
    except Exception:  # noqa: BLE001 - a cache that cannot answer
        return None


def current(*, now: float | None = None, get=None) -> SiteAgents | None:
    """The site agents to act on now, or None (fail closed). Never raises."""
    from django.core.cache import cache

    t = time.time() if now is None else now
    try:
        config = conf.get_host_config()
    except Exception:  # noqa: BLE001 - HostNotConfigured or a malformed key
        log.warning("CANOPY_HOST is not usable; showing no agent", exc_info=True)
        return None
    fresh, last_good, failed = _keys(config)
    entry = cache.get(fresh)
    if _usable(entry):
        return SiteAgents.from_claims(entry["claims"], age=t - entry["fetched_at"])
    if not cache.get(failed):
        try:
            return refresh(now=t, get=get)
        except SiteAgentsError as exc:
            log.warning("canopy's site agents could not be refreshed (%s): %s",
                        getattr(exc, "code", "not_configured"), exc)
        except Exception:  # noqa: BLE001 - nothing here may take a page down
            log.exception("canopy's site agents could not be refreshed")
        cache.set(failed, 1, RETRY_SECONDS)
    entry = cache.get(last_good)
    if _usable(entry) and t - entry["fetched_at"] <= STALE_SECONDS:
        return SiteAgents.from_claims(entry["claims"], stale=True, age=t - entry["fetched_at"])
    return None


def _usable(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("claims"), dict)
            and isinstance(entry.get("fetched_at"), (int, float)))

