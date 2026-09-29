"""The live probe's clock: every runner's session report (`sessions_reported`).

canopy-web has no job scheduler. Every ~10s some runner reports its sessions,
and the Slack and chat status sweeps already ride that report for the same
reason; `live_probe.sweep` rides it too, behind its own fleet-wide cache lock,
and probes at most one overdue Connected site per minute, in the background.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import sessions_reported

log = logging.getLogger(__name__)


@receiver(sessions_reported, dispatch_uid="tokens_live_probe_sweep")
def _live_probe_sweep(sender, runner, **kwargs):
    from .live_probe import sweep

    try:
        sweep()
    except Exception:  # noqa: BLE001 - never break a runner's report over a probe
        log.exception("live-probe sweep failed")
