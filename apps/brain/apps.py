from django.apps import AppConfig


class BrainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.brain"
    label = "brain"

    def ready(self) -> None:
        from apps.common import ticks  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        from . import signals  # noqa: F401, PLC0415  (an activity fold schedules association)
        from . import targets  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        # Links are made off-request: the fold only marks a source due (`associate.schedule`).
        ticks.register("brain.associate", _associate, 60)
        # A removal reaches what was derived from it within one sweep (`cascade.sweep`), and the read record
        # is compacted into each person's disclosure summary (`disclosure.compact`).
        ticks.register("brain.cascade", _cascade, 15 * 60)
        ticks.register("brain.disclosure", _disclosure, 60 * 60)
        # A learner runs where it is aimed, on each target's own canopy schedule: its fire starts a brain run.
        targets.register()
        # A person's record of what was read of their words, and of what they agreed to, outlives the event log's
        # pruning: the disclosure summary is recomputed from the reads, and the cascade asks who ever agreed.
        from apps.events import services as events  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        from . import consent, readers  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        events.keep(readers.EVENT_SOURCE, "a person's disclosure summary is recomputed from these (apps/brain)")
        events.keep(consent.SOURCE, "who ever agreed to a learning class (apps/brain/cascade.ever_agreed)")


def _associate() -> None:
    from . import associate  # noqa: PLC0415 — models: the app registry is not ready at module load

    associate.run_due()


def _cascade() -> None:
    from . import cascade  # noqa: PLC0415 — models: the app registry is not ready at module load

    cascade.sweep()


def _disclosure() -> None:
    from . import disclosure  # noqa: PLC0415 — models: the app registry is not ready at module load

    disclosure.compact()
