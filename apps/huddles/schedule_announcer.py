"""How a project check-in's schedule fires: registered into `apps.harness.schedule_hooks`."""
from __future__ import annotations

from apps.harness import schedule_hooks

from . import checkins
from .models import CheckinRun, ProjectCheckin


class _CheckinAnnouncer:
    def owns(self, schedule) -> bool:
        return ProjectCheckin.objects.filter(schedule_id=schedule.pk).exists()

    def announce(self, schedule, slot, *, manual: bool = False):
        return checkins.announce(schedule, slot, manual=manual)

    def run_for(self, schedule, slot):
        return CheckinRun.objects.filter(checkin__schedule=schedule, slot=slot).first()


def register() -> None:
    schedule_hooks.register(_CheckinAnnouncer())
