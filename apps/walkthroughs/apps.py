from django.apps import AppConfig


class WalkthroughsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.walkthroughs"

    def ready(self) -> None:
        # Contribute the published-demo count to the framework's public stats.
        # Registered from HERE, not imported from there: apps/system is FRAMEWORK
        # and must not name a PRODUCT module (tests/test_architecture_boundary.py).
        # In ready() so it runs once, after the app registry is populated.
        from apps.system.stats import register_extra_stat

        from .models import Walkthrough

        def demos_published() -> int:
            # A "run package" is a derived grouping of Walkthrough rows by
            # run_id (apps/runs/aggregate.py) — apps/runs has no models. Counting
            # rows would inflate it: one package is a video AND a deck.
            return (
                Walkthrough.objects.exclude(run_id__isnull=True)
                .exclude(run_id="")
                .values("run_id")
                .distinct()
                .count()
            )

        register_extra_stat("demos_published", demos_published)
