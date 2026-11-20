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
        # A removal reaches what was derived from it within one sweep (`cascade.sweep`), and the read record
        # is compacted into each person's disclosure summary (`disclosure.compact`).
        ticks.register("brain.cascade", _cascade, 15 * 60)
        ticks.register("brain.disclosure", _disclosure, 60 * 60)
        # Learning runs start off-request too, per active project, only when there is something new.
        ticks.register("brain.schedule", _schedule, 300)
        # A preview snapshot is kept a week; a removal reaches it with every cascade sweep (`snapshots.sweep`).
        ticks.register("brain.previews", _previews, 60 * 60)
        from . import cascade, snapshots  # noqa: PLC0415 — as above

        cascade.register_store(snapshots.KIND, snapshots.sweep, by_session=True)


def _associate() -> None:
    from . import associate  # noqa: PLC0415 — models: the app registry is not ready at module load

    associate.run_due()


def _cascade() -> None:
    from . import cascade  # noqa: PLC0415 — models: the app registry is not ready at module load

    cascade.sweep()


def _disclosure() -> None:
    from . import disclosure  # noqa: PLC0415 — models: the app registry is not ready at module load

    disclosure.compact()

def _schedule() -> None:
    from . import schedule  # noqa: PLC0415 — models: the app registry is not ready at module load

    schedule.run_due()


def _previews() -> None:
    from . import snapshots  # noqa: PLC0415 — models: the app registry is not ready at module load

    snapshots.expire()
