"""A turn's idempotency key is unique per workspace (`idempotency_scope`), not
globally. Existing rows get their workspace as their scope."""

from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def fill_scope(apps, schema_editor):
    Turn = apps.get_model("harness", "Turn")
    Agent = apps.get_model("agents", "Agent")
    Session = apps.get_model("canopy_sessions", "Session")
    Turn.objects.filter(agent__isnull=False).update(idempotency_scope=Subquery(
        Agent.objects.filter(pk=OuterRef("agent_id")).values("workspace_id")[:1]))
    Turn.objects.filter(chat_session__isnull=False).update(idempotency_scope=Subquery(
        Session.objects.filter(pk=OuterRef("chat_session_id")).values("workspace_id")[:1]))
    Turn.objects.filter(agent__isnull=True, chat_session__isnull=True,
                        workspace__isnull=False).update(idempotency_scope=models.F("workspace_id"))


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0094_runner_kind_computer"),
        ("agents", "0071_credential_source_labels"),
        ("canopy_sessions", "0046_retitle_uuid_named_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="turn",
            name="idempotency_scope",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="turn",
            name="idempotency_key",
            field=models.CharField(max_length=128),
        ),
        migrations.RunPython(fill_scope, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="turn",
            constraint=models.UniqueConstraint(
                fields=("idempotency_key", "idempotency_scope"),
                name="uniq_turn_idempotency_key_per_scope",
            ),
        ),
    ]
