"""Aggregate counts for the public explainer page.

Aggregates ONLY — no names, no slugs, no ids, no per-agent or per-tenant
breakdown, no content. A count cannot leak what it is a count of, which is the
whole reason this is safe to serve anonymously. Adding a non-integer field here
is a security change, and tests/test_public_stats.py fails on the type rather
than on a denylist of names so that it cannot be done by accident.

That said, "a count cannot leak what it is a count of" is the right frame for
*identity*, not for everything else these numbers can carry. Considered and
accepted:
  - Five monotonic global counters, pollable at 60s granularity, reveal fleet
    ACTIVITY: throughput per minute, whether any box is currently up at all
    (effectively presence for a small named team), and the moment a demo
    package gets created (a step change in `demos_published`).
  - `demos_published` counts distinct `run_id`s across ALL tenants, but only
    `link`-visibility walkthroughs — a `private` run does NOT increment the
    public total. Now that canopy is open to a second tenant, folding another
    tenant's private packages into a number the internet can see would be a
    disclosure made on their behalf without asking, even though the count
    itself carries no name/slug/id. See apps/walkthroughs/apps.py.
Neither is a names/slugs/ids leak, so neither changes the type-closed
contract this module enforces. But "aggregates are safe" is not an
unconditional license — a future stat that narrows the denominator (e.g. "per
customer" instead of "global") would reopen exactly the question this module
was written to close.

No Django request object — pure functions over the ORM, so they are testable
directly.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import HEARTBEAT_ONLINE_WINDOW, Runner, Turn

from . import reader

logger = logging.getLogger(__name__)

#: Product apps contribute counts here from their AppConfig.ready(). This app is
#: FRAMEWORK and must not import PRODUCT (tests/test_architecture_boundary.py),
#: so the arrow has to point inward: product names framework, never the reverse.
#: Same reason apps/timeline reads product events through a registry.
_EXTRA: dict[str, Callable[[], int]] = {}


def register_extra_stat(name: str, fn: Callable[[], int]) -> None:
    """Register a product-owned count. Idempotent — re-registering replaces."""
    _EXTRA[name] = fn


def _skill_count() -> int:
    try:
        cat = reader.load_catalog(settings.CANOPY_PLUGIN_PATH)
        return int(cat.get("counts", {}).get("skill", 0))
    except Exception:  # noqa: BLE001 — a missing plugin path must not 500 a public page
        return 0


def _runners_online() -> int:
    """Runners with a fresh heartbeat, ONLINE, and not paused.

    `Runner.live_status` is a PROPERTY, not a column, so it cannot be filtered
    on directly — but its ONLINE branch is fully reproducible in SQL, which is
    what this does: a fresh heartbeat (the same 90-second HEARTBEAT_ONLINE_WINDOW
    that live_status uses to demote a runner to STALE) on a runner whose
    self-reported `status` is ONLINE and whose `paused` column (a real field,
    not derived) is False.

    Excluding RETIRED alone is not enough: pausing a box is routine in this
    fleet (token exhaustion -> shift to the next account), and a paused or
    degraded runner still heartbeats — so counting on heartbeat-freshness alone
    would inflate "online" exactly when the fleet is degraded, which is the one
    time this number matters most. Deliberately NOT `is_available` — readiness
    (whether it's REPORTING ITSELF ready to claim work) is an operational
    detail; "how many boxes are up and not deliberately parked" is the honest
    public number.
    """
    cutoff = timezone.now() - HEARTBEAT_ONLINE_WINDOW
    return Runner.objects.filter(
        status=Runner.ONLINE, paused=False, last_heartbeat_at__gte=cutoff
    ).count()


def _extra(name: str) -> int:
    """A registered product count, or 0 if that app did not register one.

    Defaulting to 0 rather than omitting the key keeps PublicStatsOut's field set
    CLOSED, which is what the leak test asserts — a dynamic response shape would
    make "only integers, only these keys" unenforceable.
    """
    fn = _EXTRA.get(name)
    if fn is None:
        # CI catches a de-registration today (a coverage test would fail), but
        # an INSTALLED_APPS change plus a matching test edit would not — log so
        # there's a signal even then.
        logger.warning("public stats contributor %r is not registered", name)
        return 0
    try:
        return int(fn())
    except Exception:  # noqa: BLE001 — one bad contributor must not 500 a public page
        # Logged, not silent: a contributor that silently fails (e.g. after a
        # field rename in the registering app) would otherwise report 0
        # forever with no signal that anything is wrong.
        logger.exception("public stats contributor %r failed", name)
        return 0


#: Short TTL on an ANONYMOUS endpoint, so an unauthenticated caller cannot turn
#: this into a free load generator against the database. 60s is well inside
#: what a counts display needs to be truthful. In production
#: (config/settings/connectlabs.py) `CACHES` points at the shared ElastiCache
#: Redis whenever `REDIS_URL` is set, so this is one recompute per 60s for the
#: WHOLE FLEET, not per process — a better bound than a per-process cache would
#: give. Locally/in tests, with no `CACHES` configured, Django falls back to
#: its default per-process LocMemCache. Either way `public_stats()` below
#: treats the cache as unreliable (network-backed in prod) and never lets a
#: cache outage 500 this page.
_CACHE_KEY = "system:public-stats:v1"
_CACHE_TTL = 60


def _compute() -> dict[str, int]:
    return {
        "agents": Agent.objects.count(),
        "skills": _skill_count(),
        "runners_online": _runners_online(),
        # DONE only — "turns executed" should not count failures or cancellations.
        # Hits the ("status", "created_at") index on its leading column.
        "turns_executed": Turn.objects.filter(status=Turn.DONE).count(),
        "demos_published": _extra("demos_published"),
    }


def public_stats() -> dict[str, int]:
    try:
        cached = cache.get(_CACHE_KEY)
    except Exception:  # noqa: BLE001 — Redis in prod; an outage must not 500 a public page
        return _compute()
    if cached is not None:
        return cached
    stats = _compute()
    try:
        cache.set(_CACHE_KEY, stats, timeout=_CACHE_TTL)
    except Exception:  # noqa: BLE001 — same: a failed cache WRITE must not 500 either
        pass
    return stats
