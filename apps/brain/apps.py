from django.apps import AppConfig


class BrainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.brain"
    label = "brain"

    def ready(self) -> None:
        from apps.common import ticks  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        from . import signals  # noqa: F401, PLC0415  (an activity fold schedules association)

        # Links are made off-request: the fold only marks a source due (`associate.schedule`).
        ticks.register("brain.associate", _associate, 60)
        # Learning runs start off-request too, per active project, only when there is something new.
        ticks.register("brain.schedule", _schedule, 300)


def _associate() -> None:
    from . import associate  # noqa: PLC0415 — models: the app registry is not ready at module load

    associate.run_due()


def _schedule() -> None:
    from . import schedule  # noqa: PLC0415 — models: the app registry is not ready at module load

    schedule.run_due()
