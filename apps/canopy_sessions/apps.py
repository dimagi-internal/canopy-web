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
        # A transfer whose source never answers goes on without its snapshot.
        ticks.register("canopy_sessions.settle_handoffs", _settle_handoffs, 30)
        # A private conversation's page no turn ever ends for is forgotten after a while.
        ticks.register("canopy_sessions.forget_idle_sensitive_pages", _forget_idle_sensitive_pages, 60)
        # A private conversation's file is held for its agent no longer than its turn's words.
        ticks.register("canopy_sessions.private_attachments", _sweep_private_attachments, 60)


def _purge_expired_secrets() -> None:
    from .secrets import purge_expired

    purge_expired()


def _settle_handoffs() -> None:
    from .handoff import settle_overdue  # noqa: PLC0415 — models: the app registry is not ready at module load

    settle_overdue()


def _forget_idle_sensitive_pages() -> None:
    from .page_state import forget_idle_sensitive_pages  # noqa: PLC0415 — models: the app registry is not ready at module load

    forget_idle_sensitive_pages()


def _sweep_private_attachments() -> None:
    from .private_attachments import sweep  # noqa: PLC0415 — models: the app registry is not ready at module load

    sweep()
