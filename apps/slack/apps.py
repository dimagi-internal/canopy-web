from django.apps import AppConfig


class SlackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.slack"
    label = "slack"

    def ready(self) -> None:
        # Posts agent replies back into the Slack thread (apps/slack/relay.py).
        from apps.slack import signals  # noqa: F401
