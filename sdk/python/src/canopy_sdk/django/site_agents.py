"""``current()`` — canopy's site agents for this Django host, cached, failing closed.

::

    from canopy_sdk.django import site_agents

    current = site_agents.current()
    if current is None:
        ...                       # show no agent: canopy has said nothing for a day
    elif current.stale:
        ...                       # canopy unreachable; this is its last word, current.age seconds old
    agent = current.agent("labsdata") if current else None

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
address) and ``AGENT_SLUG`` (which tenant's registration of the name).
"""
from __future__ import annotations

import hashlib
import logging
import time

from ..host.client_keys import ClientKeyResolver
from ..host.config import HostNotConfigured
from ..site_agents import SiteAgents, SiteAgentsError, fetch_site_agents
from . import conf

log = logging.getLogger("canopy_sdk.django")

FRESH_SECONDS = 300
STALE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60


def _keys(config) -> tuple[str, str, str]:
    scope = hashlib.sha256(f"{config.canopy_base_url}|{config.app_name}".encode()).hexdigest()[:16]
    return (f"canopy_sdk:site_agents:{scope}:fresh", f"canopy_sdk:site_agents:{scope}:last_good",
            f"canopy_sdk:site_agents:{scope}:failed")


def refresh(*, now: float | None = None, get=None) -> SiteAgents:
    """Fetch and verify the site agents now, and cache it. Raises ``SiteAgentsError`` or
    ``HostNotConfigured``."""
    from django.core.cache import cache

    config = conf.get_host_config()
    if not config.assertions_enabled:
        raise HostNotConfigured("CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME to ask for its agents")
    fresh, last_good, failed = _keys(config)
    listing = fetch_site_agents(config.canopy_base_url, config.app_name, config.signing_key,
                        ClientKeyResolver(cache=cache),
                        client_id=config.canopy_client_id or None, agent=conf.agent_slug(), get=get)
    entry = {"claims": listing.claims, "fetched_at": time.time() if now is None else now}
    cache.set(fresh, entry, FRESH_SECONDS)
    cache.set(last_good, entry, STALE_SECONDS + FRESH_SECONDS)
    cache.delete(failed)
    return listing


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
        except (SiteAgentsError, HostNotConfigured) as exc:
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

