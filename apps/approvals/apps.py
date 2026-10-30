from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.approvals"

    def ready(self) -> None:
        from apps.common import ticks  # noqa: PLC0415 — ready(): the app registry is loaded

        from . import signals  # noqa: F401, PLC0415 — registers the receivers

        # The backstop: a card whose delivery was lost (a restart between the commit and the
        # send) is delivered within a minute. New cards are delivered on commit, not here.
        ticks.register("approvals.backstop", _backstop, 60)


def _backstop() -> None:
    from .delivery import deliver_missed  # noqa: PLC0415 — models are not importable at module load

    deliver_missed()
