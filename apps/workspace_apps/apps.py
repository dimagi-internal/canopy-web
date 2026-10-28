import logging

from django.apps import AppConfig

from apps.common import ticks


class WorkspaceAppsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workspace_apps"

    def ready(self) -> None:
        # Apps' own retention rules (a candidate's data past its keep date), twice a day.
        ticks.register("workspace_apps.retention", _retention, 12 * 60 * 60)
        # Emails an action held back (a rejection sent the next day), every ten minutes.
        ticks.register("workspace_apps.scheduled_email", _scheduled_email, 10 * 60)


def _retention() -> None:
    from .services import retention_sweep  # noqa: PLC0415 — models: the app registry is not ready at module load

    try:
        retention_sweep()
    except Exception:  # noqa: BLE001 — a failed sweep must not stop the clock's other jobs
        logging.getLogger(__name__).exception("workspace apps: retention sweep failed")


def _scheduled_email() -> None:
    from .services import send_scheduled  # noqa: PLC0415 — models: the app registry is not ready at module load

    try:
        send_scheduled()
    except Exception:  # noqa: BLE001 — a failed run must not stop the clock's other jobs
        logging.getLogger(__name__).exception("workspace apps: scheduled emails failed")
