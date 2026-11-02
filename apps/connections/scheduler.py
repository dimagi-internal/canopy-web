"""The connections sweep's clock: a job on the off-request clock (apps/common/ticks.py).

`keeper.sweep` deletes what no holder will ever finish (a `revoking` row nobody
confirmed in seven days, or that no cloud box holds any more) and the removed apps
left with no connection. Nothing about it is urgent — the rows are already hidden
and handed to no machine — so it runs every SWEEP_EVERY_S, deduplicated fleet-wide
by the clock (registered from ConnectionsConfig.ready, as the capabilities sweep is).
`manage.py connections_sweep` runs it by hand.
"""
from __future__ import annotations

from apps.common import ticks

#: How often the sweep runs.
SWEEP_EVERY_S = 15 * 60


def run() -> None:
    from . import keeper  # noqa: PLC0415 — imported from AppConfig.ready; models load later

    keeper.sweep()


def register() -> None:
    ticks.register("connections.sweep", run, SWEEP_EVERY_S)
