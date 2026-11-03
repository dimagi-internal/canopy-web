from django.apps import AppConfig


class HarnessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.harness"
    verbose_name = "Agent execution harness"

    def ready(self) -> None:
        from . import departure  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        departure.register()
        from apps.workspaces import role_cap  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        role_cap.register_turn_origin(_turn_origin_ref)
        # Importing is what CONNECTS the page-invalidation receiver. A signals
        # module nothing imports is dead code that looks alive — how
        # `apps/mcp/page_tools.py` shipped with ten passing tests and no
        # feature. `tests/test_item_invalidation` asserts against the connected
        # receiver, not the function, so this line cannot go silently.
        from . import signals  # noqa: F401
        # The harness's periodic jobs, on the off-request clock (apps/common/ticks.py):
        # the queue sweeps and scheduled unpauses (sweeps.py), and the schedule clock.
        from apps.common import ticks

        from . import fleets, schedule_clock, sweeps

        sweeps.register()
        fleets.register()
        ticks.register("harness.schedule_clock", schedule_clock.run, schedule_clock.interval_s, dedupe=False)
        # Act-as-you requests and permissions that ran out are written `expired` (and
        # recorded) within a minute, whether or not anyone reads them again.
        from . import act_as  # noqa: PLC0415 — AppConfig.ready() wiring, after the app registry is loaded

        ticks.register("harness.act_as_expire", act_as.expire_due, 60)


def _turn_origin_ref(turn_id, user):
    from .models import Turn  # noqa: PLC0415 — models load only after the app registry

    return (Turn.objects.filter(pk=turn_id, initiator_user=user)
            .values_list("origin_ref", flat=True).first())
