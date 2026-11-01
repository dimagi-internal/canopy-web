"""Mark the USER rows a person typed (`Message.typed`) in every session that already
holds rows, by the rule new rows are marked with as they are stored
(`transcript_read.mark_typed`). Only sets a column on `Message`; nothing references
a Message row's `typed`, so it updates no referenced key.

Runs the live rule rather than a copy of it, because the rule reads the session's
turns and tool rows the way the ingest path does. Once a deploy has run it, this
migration guards nothing and its body may become a no-op."""
from django.db import migrations

#: Sessions per batch, so one transaction never holds the whole table's rows.
BATCH = 200


def backfill(apps, schema_editor):
    # Historical models for the rows read, so a later column on Session or Message
    # never breaks this step; `mark_typed` reads only fields present here.
    Session = apps.get_model("canopy_sessions", "Session")
    Message = apps.get_model("canopy_sessions", "Message")
    session_ids = list(
        Message.objects.filter(role="user", typed=False)
        .values_list("session_id", flat=True).distinct()
    )
    if not session_ids:
        return
    from apps.canopy_sessions.transcript_read import mark_typed

    for start in range(0, len(session_ids), BATCH):
        for session in Session.objects.filter(pk__in=session_ids[start:start + BATCH]):
            mark_typed(session, Message.objects.filter(session=session, role="user", typed=False)
                       .values_list("turn_index", flat=True))


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0064_message_typed")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
