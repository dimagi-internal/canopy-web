"""Aggregate counts for the public explainer page.

Aggregates ONLY — no names, no slugs, no ids, no per-agent or per-tenant
breakdown, no content. A count cannot leak what it is a count of, which is the
whole reason this is safe to serve anonymously. Adding a non-integer field here
is a security change, and tests/test_public_stats.py fails on the type rather
than on a denylist of names so that it cannot be done by accident.

No Django request object — pure functions over the ORM, so they are testable
directly.
"""
from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import HEARTBEAT_ONLINE_WINDOW, Runner, Turn

from . import reader

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
    except Exception:  # noqa: BLE001 — a missing plugin path must not 500 a public page
        return 0
    return int(cat.get("counts", {}).get("skill", 0))


def _runners_online() -> int:
    """Runners with a fresh heartbeat, excluding retired boxes.

    `Runner.live_status` is a PROPERTY, not a column, so it cannot be filtered
    on. This is its DB-expressible core: the same 90-second window
    (HEARTBEAT_ONLINE_WINDOW) that live_status uses to demote a runner to STALE.
    Deliberately NOT `is_available` — readiness is an operational detail and
    "how many boxes are up" is the honest public number.
    """
    cutoff = timezone.now() - HEARTBEAT_ONLINE_WINDOW
    return (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(last_heartbeat_at__gte=cutoff)
        .count()
    )


def _extra(name: str) -> int:
    """A registered product count, or 0 if that app did not register one.

    Defaulting to 0 rather than omitting the key keeps PublicStatsOut's field set
    CLOSED, which is what the leak test asserts — a dynamic response shape would
    make "only integers, only these keys" unenforceable.
    """
    fn = _EXTRA.get(name)
    if fn is None:
        return 0
    try:
        return int(fn())
    except Exception:  # noqa: BLE001 — one bad contributor must not 500 a public page
        return 0


#: Short TTL on an ANONYMOUS endpoint, so an unauthenticated caller cannot turn
#: this into a free load generator against the database. 60s is well inside what
#: a counts display needs to be truthful. The project configures no `CACHES`, so
#: this is Django's default per-process LocMemCache — each web process computing
#: the counts once a minute is fine, and the pattern matches
#: `apps/canopy_sessions/attach.py` and `apps/mcp/rate_limit.py`.
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
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    stats = _compute()
    cache.set(_CACHE_KEY, stats, timeout=_CACHE_TTL)
    return stats
