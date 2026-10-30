from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.approvals"

    def ready(self) -> None:
        from apps.common import ticks

        from . import signals  # noqa: F401  — registers the receivers

        # The backstop: a card whose delivery was lost (a restart between the commit and the
        # send) is delivered within a minute. New cards are delivered on commit, not here.
        ticks.register("approvals.backstop", _backstop, 60)


def _backstop() -> None:
    from .delivery import deliver_missed

    deliver_missed()
