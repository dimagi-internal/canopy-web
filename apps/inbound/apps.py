from django.apps import AppConfig


class InboundConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inbound"
    label = "inbound"

    def ready(self) -> None:
        # Push-miss auditing. Unlike apps.events (which must stay inert), this
        # receiver only WRITES A LOG ROW — it never creates work.
        from apps.inbound import signals  # noqa: F401
