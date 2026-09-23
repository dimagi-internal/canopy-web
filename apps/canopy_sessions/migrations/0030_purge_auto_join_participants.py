"""Remove the participant rows the chat socket's old auto-join wrote.

Until #926 (2026-09-23) opening a chat's socket added any workspace member as
an EDITOR participant. Because the ACL honours participant rows, a co-tenant
who had once opened a teammate's chat kept permanent access to it, even though
it was never shared with them.

Removed: an EDITOR row for someone other than the chat's creator, created
before the People control shipped, on a chat that is not tied to a Slack
thread. Slack is the one path that legitimately added members to other
people's chats (they could already read the thread in Slack), and its sessions
carry `slack_channel` in metadata.

Kept: the creator's OWNER row, every Slack-thread participant, and any row
written after the cutoff (a real share through the People control). Anyone
who still reaches a chat another way (a runner-discovered session, or an agent
they run) keeps reaching it; losing the row changes nothing for them.

Not reversible: the deleted rows cannot be told apart from anything else.
"""
import datetime as dt

from django.db import migrations

# #926 deployed after this, so no People-control share predates it.
CUTOFF = dt.datetime(2026, 9, 23, 20, 0, tzinfo=dt.timezone.utc)


def purge(apps, schema_editor):
    SessionParticipant = apps.get_model("canopy_sessions", "SessionParticipant")
    rows = (SessionParticipant.objects
            .filter(role="editor", created_at__lt=CUTOFF)
            .exclude(session__metadata__has_key="slack_channel"))
    doomed = [p.pk for p in rows.select_related("session")
              if p.user_id != p.session.created_by_id]
    SessionParticipant.objects.filter(pk__in=doomed).delete()
    print(f"\n  purged {len(doomed)} auto-join participant row(s)")


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0029_binding_missed_reports")]
    operations = [migrations.RunPython(purge, migrations.RunPython.noop)]
