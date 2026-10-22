from django.apps import AppConfig


class AdmissionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.admission"
    label = "admission"

    def ready(self) -> None:
        # Seeds the default rules into every NEW workspace (existing ones were
        # seeded by migration 0002).
        from . import signals  # noqa: F401
        # A queued turn nothing will run is rejected and its owner told; sender
        # notices no runner sent within a day end `failed`. Both off-request.
        from apps.common import ticks

        ticks.register("admission.sweep_unrunnable", _sweep_unrunnable, 60)
        ticks.register("admission.expire_runner_notices", _expire_runner_notices, 60)


def _sweep_unrunnable() -> None:
    from django.utils import timezone

    from .evaluate import sweep_unrunnable

    sweep_unrunnable(timezone.now())


def _expire_runner_notices() -> None:
    from django.utils import timezone

    from .runner_notices import maybe_expire

    maybe_expire(timezone.now())
