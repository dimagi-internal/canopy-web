from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.approvals"

    def ready(self) -> None:
        from apps.common import ticks  # noqa: PLC0415 — ready(): the app registry is loaded

        # Delivery happens on commit of the filing; this is the backstop for one that was lost (a
        # restart between the commit and the send), and the clock that expires what nobody decided.
        ticks.register("approvals.backstop", _backstop, 60)


def _backstop() -> None:
    from . import delivery, services  # noqa: PLC0415 — models are not importable at module load

    delivery.deliver_missed()
    services.expire_due()
