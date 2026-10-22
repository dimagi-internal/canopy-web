from django.apps import AppConfig


class SlackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.slack"
    label = "slack"

    def ready(self) -> None:
        # Posts agent replies back into the Slack thread (apps/slack/relay.py).
        from apps.slack import signals  # noqa: F401

        # Moving an agent revokes and re-homes its channel ingests (agent_move.py).
        # Registered from here, as apps/mail does, so the move needs no Slack import.
        from apps.agents import move

        from .agent_move import participant

        move.register(participant)
        # Approved channel reads, on the off-request clock (ingest_scheduler.py).
        from . import ingest_scheduler

        ingest_scheduler.register()
