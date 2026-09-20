"""A task can carry an ask — the thing an `Item` used to be.

Hand-written rather than generated for one reason: `uuid` is UNIQUE, and
`makemigrations` would ask for a single one-off default to give every existing
row, which is exactly the collision uniqueness forbids. So it is added nullable,
filled per row, and only then made unique.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fill_uuids(apps, schema_editor):
    AgentTask = apps.get_model("agents", "AgentTask")
    for pk in AgentTask.objects.filter(uuid__isnull=True).values_list("pk", flat=True).iterator():
        AgentTask.objects.filter(pk=pk).update(uuid=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("agents", "0026_agent_project_seq"),
        ("harness", "0043_turn_initiator"),
    ]

    operations = [
        migrations.AddField(
            model_name="agenttask",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, null=True, editable=False),
        ),
        migrations.RunPython(fill_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="agenttask",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="waiting_on_user",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks_waiting_on", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="ask_kind",
            field=models.CharField(
                blank=True, default="", max_length=10,
                choices=[("", "None"), ("review", "Review"), ("question", "Question")],
            ),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="ask_body",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="decision",
            field=models.CharField(
                blank=True, default="", max_length=10,
                choices=[("implement", "Implement"), ("skip", "Skip"), ("defer", "Defer")],
            ),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="decided_by",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="decided_by_user",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks_decided", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="decided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="dispatch",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="batch_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="origin",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="origin_ref",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="agenttask",
            name="raised_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="raised_tasks", to="harness.turn",
            ),
        ),
    ]
