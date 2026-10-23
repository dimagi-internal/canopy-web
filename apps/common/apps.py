from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"

    def ready(self):
        from . import process_stats, ticks  # noqa: PLC0415 — app modules load after the registry

        # Every web task reports on itself, so no fleet-wide dedupe.
        ticks.register("common.process_stats", process_stats.run, process_stats.interval_s, dedupe=False)
