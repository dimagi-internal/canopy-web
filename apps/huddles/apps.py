from django.apps import AppConfig


class HuddlesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.huddles"
    label = "huddles"

    def ready(self) -> None:
        # A check-in's schedule makes a run and announces it instead of enqueuing a turn;
        # the harness asks this announcer (apps/harness/schedule_hooks.py).
        from . import schedule_announcer  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        schedule_announcer.register()
