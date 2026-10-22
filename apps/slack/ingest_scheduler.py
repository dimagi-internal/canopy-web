"""The channel-ingest read's own clock: an asyncio task in every web process.

An approved ingest (apps/slack/ingest.py) reads a whole channel — thousands of
messages, paged, and paused whenever Slack rate-limits — which cannot happen
inside the 3 seconds Slack gives the Approve click. canopy-web has no worker or
queue, so it is a job on the off-request clock (apps/common/ticks.py, registered
from SlackConfig.ready) that calls `ingest.tick` every SLACK_INGEST_TICK_S.
`tick` leases each ingest it takes, so N web tasks never read the same one at
once.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def interval_s() -> float:
    return float(getattr(settings, "SLACK_INGEST_TICK_S", 15) or 0)


def run() -> None:
    from . import ingest

    ingest.tick()
    _expire_group_runs()


def _expire_group_runs() -> None:
    """Check-in announcements nobody confirmed lapse after 24 h (apps/huddles/checkins.py)
    — on this loop because it is the one that already reads channels for them."""
    from apps.huddles import checkins

    try:
        checkins.expire_due()
    except Exception:  # noqa: BLE001 — never stop the reads over it
        logger.exception("check-in expiry failed")


def register() -> None:
    from apps.common import ticks

    ticks.register("slack.channel_ingest", run, interval_s, dedupe=False)
