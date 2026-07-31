"""Collapse the pre-lifecycle Sessions backlog.

Until now nothing could ever retire a session row, so labs accumulated one per emdash
task any runner ever reported — most of them tasks that no longer exist. Apply the
staleness rule once so the list starts clean.

Irreversible by design, and safe to be: un-archiving happens naturally on the next
report, so the reverse is a genuine no-op rather than lost information.

The rule is INLINED, deliberately. This migration used to call
`staleness.archive_stale_sessions`, and that broke the moment the live predicate
learned to cross into `runner_binding__runner__sessions_reported_at`: the historical
Runner this migration is handed has no such field, so every test database failed to
build. A one-shot backfill wants the rule AS IT WAS when it ran, not whatever the rule
becomes years later — so it carries its own copy and stops tracking a moving target.
`SESSION_LIVE_WINDOW` is still imported, since the window is data, not logic.
"""
from django.db import migrations
from django.db.models import Q
from django.utils import timezone

from apps.canopy_sessions.staleness import SESSION_LIVE_WINDOW


def forwards(apps, schema_editor):
    Session = apps.get_model("canopy_sessions", "Session")
    cutoff = timezone.now() - SESSION_LIVE_WINDOW
    unseen = Q(origin="runner") & (
        Q(runner_binding__live_seen_at__lt=cutoff)
        | Q(runner_binding__live_seen_at__isnull=True)
    )
    Session.objects.filter(status="active").filter(unseen).update(status="archived")


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0008_runnerbinding_backfill_requested"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
