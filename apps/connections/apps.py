from django.apps import AppConfig


class ConnectionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.connections"
    label = "connections"
    verbose_name = "Connections"

    def ready(self) -> None:
        # Moving an agent says which of its connections stop working
        # (apps/agents/move.py). The move is core and cannot import this app, so
        # the app registers itself, as `apps.mail` does.
        from apps.agents import move  # noqa: PLC0415 - registry; models must be loaded first

        from .agent_move import participant  # noqa: PLC0415 - same

        move.register(participant)
