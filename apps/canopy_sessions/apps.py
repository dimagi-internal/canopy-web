from django.apps import AppConfig


class CanopySessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.canopy_sessions"
    label = "canopy_sessions"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (Message projection receiver)
        from . import autotitle  # noqa: F401  (auto-title receiver)
        # Shared chat secrets past their 30 minutes are deleted off-request, so the
        # ciphertext goes even if nobody reads the chat again.
        from apps.common import ticks

        ticks.register("canopy_sessions.purge_expired_secrets", _purge_expired_secrets, 60)


def _purge_expired_secrets() -> None:
    from .secrets import purge_expired

    purge_expired()
