from django.apps import AppConfig

from apps.common import ticks


class ListenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.listen"
    label = "listen"

    def ready(self) -> None:
        # Ends sessions nobody joined, whose peer left, or that ran too long; a
        # socket can't end a session when no socket is left to notice.
        from . import services  # noqa: PLC0415 — services imports models, which the app registry is not ready for at module load

        ticks.register("listen.sweep", services.sweep, services.SWEEP_EVERY_S)
