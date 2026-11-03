from django.apps import AppConfig


class PushConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.push"

    def ready(self) -> None:
        from . import signals  # noqa: F401  — registers the post_save receivers
        # Delayed "your chat has gone quiet" pushes, drained off-request.
        from apps.common import ticks

        from apps.workspaces import access_requests  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        access_requests.register_push(_push_to_user)
        ticks.register("push.due_session_pushes", _send_due_session_pushes, 15)


def _send_due_session_pushes() -> None:
    from .services import send_due_session_pushes

    send_due_session_pushes()


def _push_to_user(user, title, body, path) -> int:
    from .services import send_to_user  # noqa: PLC0415 — models: the app registry is not ready at module load

    return send_to_user(user, title, body, path)
