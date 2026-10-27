from django.apps import AppConfig


class AgentTasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent_tasks"
    label = "agent_tasks"

    def ready(self) -> None:
        # Imported here, once the app registry is ready: the models they read load first.
        from apps.common import ticks  # noqa: PLC0415 — see above

        # The "responded too soon" backstop (signals.py), and tasks past their
        # deadline failing off-request (apps/common/ticks.py).
        from . import services, signals  # noqa: F401, PLC0415 — see above

        ticks.register("agent_tasks.expire", services.expire_overdue, services.EXPIRE_EVERY_S)
