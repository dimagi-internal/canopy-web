"""The canopy release a runner should be on — the second version a box runs.

The update doorbell keys on the runner's CODE (`Runner.expected_code_sha`), which a
canopy-web deploy moves. The canopy plugin + CLI is a separate program released from
dimagi-internal/canopy on every merge, and a cloud box only picks it up when its
bootstrap re-runs. Nothing server-side knew the current release, so a box behind on
it rang nothing and caught up only on its 24h age refresh (cloud-ec2-2 sat on 0.2.804
for hours after 0.2.805 stopped an hourly owner alert, 2026-10-27).

`expected()` is that release: canopy main's `plugins/canopy/.claude-plugin/plugin.json`,
read anonymously (canopy is public) and cached — the same file a box's marketplace
clone holds and its bootstrap installs the CLI from. Empty means UNKNOWN, never stale.

`refresh_owed()` is what the heartbeat reply turns into `refresh_pending`: a box that
reports a canopy version strictly BEHIND the release and has not bootstrapped since
canopy-web first saw that release. Ahead is a developer's install, left alone. A box
that bootstrapped after the release and is STILL behind is not asked again (its
bootstrap could not get there — a restart loop would not either); its `canopy.cli`
check says so instead.
"""
from __future__ import annotations

import logging
import re
import time

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

RAW_URL = "https://raw.githubusercontent.com/dimagi-internal/canopy/main/plugins/canopy/.claude-plugin/plugin.json"
#: Short: it can sit on a heartbeat's path once per TTL.
TIMEOUT = 2.0
TTL = 300
#: A failed read is retried sooner than a good one is refreshed.
FAIL_TTL = 60
#: How long canopy-web remembers when it first saw a release.
SEEN_TTL = 30 * 24 * 3600
CACHE_KEY = "harness:canopy_release"
_VERSION = re.compile(r"\d+\.\d+\.\d+")
#: The `canopy.cli` check detail an older box reports ("canopy 0.2.804",
#: "canopy CLI 0.2.804, but the plugin is …"): the first version in it is the CLI's.
_DETAIL = re.compile(r"^canopy (?:CLI )?(\d+\.\d+\.\d+)")


def parse(version) -> tuple[int, ...] | None:
    m = _VERSION.fullmatch(str(version or "").strip())
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def _raw_get(url: str):
    """The one GitHub read: anonymous, short. A seam so tests never reach GitHub
    (conftest `_no_live_canopy_release`)."""
    return requests.get(url, timeout=TIMEOUT)


def expected() -> str:
    """The released canopy plugin version, cached; "" when it cannot be read."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    version = ""
    try:
        resp = _raw_get(RAW_URL)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and parse(data.get("version")):
                version = str(data["version"]).strip()
    except (requests.RequestException, ValueError):
        logger.info("canopy release: could not read %s", RAW_URL, exc_info=True)
    cache.set(CACHE_KEY, version, TTL if version else FAIL_TTL)
    return version


def seen_at(version: str) -> float:
    """When canopy-web first saw `version` as the release (epoch seconds)."""
    key = f"{CACHE_KEY}:seen:{version}"
    cache.add(key, time.time(), SEEN_TTL)
    return float(cache.get(key) or time.time())


def reported(health: dict | None) -> str:
    """The canopy CLI version a box reported: its explicit `canopy_version`, else
    read from its `canopy.cli` check (a box from before the field). "" = unknown."""
    health = health or {}
    explicit = str(health.get("canopy_version") or "").strip()
    if parse(explicit):
        return explicit
    for check in health.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "canopy.cli":
            m = _DETAIL.match(str(check.get("detail") or ""))
            if m:
                return m.group(1)
    return ""


def behind(have: str, want: str) -> bool:
    """Strictly lower. Either side unknown is not behind."""
    h, w = parse(have), parse(want)
    return bool(h and w and h < w)


def refresh_owed(health: dict | None) -> bool:
    """The box is behind the release and has not bootstrapped since it appeared."""
    have = reported(health)
    if not have:
        return False  # checked first: most boxes never need the release read
    want = expected()
    if not behind(have, want):
        return False
    try:
        booted = float((health or {}).get("bootstrapped_at") or 0)
    except (TypeError, ValueError):
        booted = 0.0
    return booted < seen_at(want)
