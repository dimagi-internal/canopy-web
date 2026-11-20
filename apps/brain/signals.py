"""An activity fold marks its source due for association (`associate.schedule`), after the commit.

Observed on the `Session` save itself, so no ingest path has to remember: `activity.store` saves
`update_fields=["activity", "activity_keys"]` and that is the fold."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import associate


@receiver(post_save, sender="canopy_sessions.Session", dispatch_uid="brain_activity_fold_schedules_association")
def _schedule_after_fold(sender, instance, created, update_fields=None, **kwargs):
    if update_fields is None or "activity" not in update_fields:
        return
    session_id = instance.pk
    transaction.on_commit(lambda: associate.schedule(session_id))
