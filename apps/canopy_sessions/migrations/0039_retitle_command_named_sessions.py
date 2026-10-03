"""Rename sessions the cloud runner titled with a command.

Until #1097 a cloud agent turn's session was titled with its prompt's first line,
and for an email or catch-up turn that line is the command the runner was given
("/ace:turn --thread 1a0f24bf9b830273"). This renames the ones already written,
by the same rule the server now applies to new ones: the turn's email subject,
else its schedule's name, else the readable part of the command, else
"<Agent> turn". A session's turn is found through its binding's thread key,
`<agent slug>:<turn id>`, which is how the cloud runner records an agent turn.

Only titles that start with "/" are touched, so a title anyone chose is not.
"""
import uuid

from django.db import migrations


def retitle(apps, schema_editor):
    from apps.harness.services import readable_title

    Session = apps.get_model("canopy_sessions", "Session")
    Turn = apps.get_model("harness", "Turn")
    for session in Session.objects.filter(title__startswith="/").select_related("agent"):
        binding = getattr(session, "runner_binding", None)
        turn = None
        key = getattr(binding, "thread_key", "") or ""
        if ":" in key:
            try:
                turn = Turn.objects.filter(pk=uuid.UUID(key.split(":", 1)[1])).first()
            except ValueError:
                turn = None
        ref = (turn.origin_ref or {}) if turn else {}
        name = ""
        for field in ("subject", "schedule_name"):
            value = ref.get(field)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        name = name or readable_title(session.title)
        if not name:
            name = f"{session.agent.name} turn" if session.agent_id else "Turn"
        session.title = name[:200]
        session.save(update_fields=["title"])


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0038_rename_teleport_to_transfer_request"),
        ("harness", "0060_runner_mailboxes_readable"),
    ]

    operations = [migrations.RunPython(retitle, migrations.RunPython.noop)]
