"""Reset runner sessions' Message rows for the transcript-ordinal index space.

From spec 2026-07-24 (persist-runner-session-transcript), an origin=runner
session's Message.turn_index IS the transcript record ordinal. Rows written
before that model (send_message user rows, ledger projections, legacy
backfills) are keyed sequentially from 0 — a different index space. Left in
place they'd collide with incoming ordinals: get_or_create(session, turn_index)
would return the OLD row and silently swallow the new message.

Deleting them is safe by the new model's own premise: the laptop transcript is
the single source of truth for these sessions, so every dropped row is
re-shippable via catch-up ("everything after last_index") or backfill ("Load
full"), and the binding tail keeps the recent view meanwhile. Web sessions are
untouched. Irreversible, and deliberately so — the reverse operation would be
re-creating rows the transcript already owns.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Message = apps.get_model("canopy_sessions", "Message")
    Message.objects.filter(session__origin="runner").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0009_archive_stale_sessions"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
