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
        from apps.connections import declarations  # noqa: PLC0415 — the registry of what a screen is

        declarations.register_surface("app", _connection_surface)
        declarations.register_share_target("app", _connection_share_target)
        from django.db.models.signals import post_delete  # noqa: PLC0415 — beside what it hears

        from .models import AppInstallation  # noqa: PLC0415 — models: the app registry is not ready at module load

        post_delete.connect(_app_removed, sender=AppInstallation, dispatch_uid="workspace_apps.connection_shares")


def _connection_surface(request, ref):
    from .api import connection_surface  # noqa: PLC0415 — models: the app registry is not ready at module load

    return connection_surface(request, ref)


def _app_removed(sender, instance, **kwargs):
    """Every way an app can go: a key can be installed again, and must not inherit what was approved."""
    from apps.connections import declarations  # noqa: PLC0415 — the registry of what a screen is

    declarations.share_target_removed("app", f"{instance.workspace_id}/{instance.key}")


def _connection_share_target(ref):
    from .api import connection_share_target  # noqa: PLC0415 — models: the app registry is not ready at module load

    return connection_share_target(ref)


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
