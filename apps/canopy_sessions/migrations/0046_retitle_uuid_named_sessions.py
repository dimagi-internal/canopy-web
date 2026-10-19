"""Rename sessions whose title is a bare Claude session UUID (a cloud runner's
session key, written as the title before autotitle could run) after their first
user message, as autotitle now does."""

import uuid

from django.db import migrations

TITLE_MAX = 80


def _is_uuid(text):
    try:
        uuid.UUID(text.strip())
    except ValueError:
        return False
    return True


def retitle(apps, schema_editor):
    Session = apps.get_model("canopy_sessions", "Session")
    Message = apps.get_model("canopy_sessions", "Message")
    for session in Session.objects.filter(title__regex=r"^[0-9a-fA-F-]{36}$").iterator():
        if not _is_uuid(session.title):
            continue
        first = (
            Message.objects.filter(session=session, role="user")
            .order_by("turn_index")
            .values_list("plaintext", flat=True)
            .first()
        )
        title = " ".join((first or "").split())[:TITLE_MAX]
        if title:
            Session.objects.filter(pk=session.pk).update(title=title)


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0045_runnerbinding_close_error"),
    ]

    operations = [migrations.RunPython(retitle, migrations.RunPython.noop)]
