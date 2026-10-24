import datetime as dt

from django.apps import AppConfig

from apps.common import ticks

#: The event log's retention: a row untouched this long is dropped (`services.prune`).
EVENT_RETENTION = dt.timedelta(days=30)


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    label = "events"

    def ready(self) -> None:
        # Housekeeping, not load-bearing: coalescing keeps the table near-flat, so
        # once a day is plenty. `manage.py prune_events` runs it by hand.
        ticks.register("events.prune", _prune, 24 * 60 * 60)


def _prune() -> None:
    from .services import prune  # noqa: PLC0415 — models: the app registry is not ready at module load

    prune(older_than=EVENT_RETENTION)
