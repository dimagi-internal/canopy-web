"""Tasks and board actions in their final shape; work products dropped.

`AgentTask` loses the item-era decision fields: an ask is open while
`ask_closed_at` is null (copied from `decided_at`), and who closed it and why is
the closing `AgentTaskAction` row. `dispatch` becomes `on_approve`.

`AgentTaskCommand` becomes `AgentTaskAction`: `kind` -> `action` (mapped onto the
five actions; `edit`/`reassign` rows were field edits and are deleted),
`payload` -> `comment` (its `note`/`reason` text), `created_by` -> `by`. Rows
with no task are deleted so `task` can be required. `dismissed` rows become
`applied` (there is no third status any more: the row is no longer pending).

`AgentWorkProduct` is dropped with its rows.

Not reversible in practice: the data steps reverse as no-ops, and re-adding the
unique `uuid` column to a table with more than one row would fail exactly as
0027 once did. Restore from backup rather than migrating back past this.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

ACTION_FOR_KIND = {"accept": "approve", "decline": "decline", "comment": "reply",
                   "dispatch": "dispatch", "done": "done"}


def comment_from_payload(payload: dict) -> str:
    return str((payload or {}).get("note") or (payload or {}).get("reason") or "")


def forward_tasks(apps, schema_editor):
    Task = apps.get_model("agents", "AgentTask")  # noqa: N806
    Task.objects.filter(decided_at__isnull=False).update(ask_closed_at=models.F("decided_at"))


def forward_actions(apps, schema_editor):
    Action = apps.get_model("agents", "AgentTaskAction")  # noqa: N806
    Action.objects.filter(task__isnull=True).delete()
    Action.objects.exclude(action__in=list(ACTION_FOR_KIND)).delete()
    Action.objects.filter(status="dismissed").update(status="applied")
    for row in Action.objects.all().iterator():
        row.action = ACTION_FOR_KIND[row.action]
        row.comment = comment_from_payload(row.payload)
        row.save(update_fields=["action", "comment"])


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0037_copy_repo_identity_from_projects"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # (1)-(4) AgentTask: the ask closes on a timestamp, the decision fields go.
        migrations.AddField(
            model_name="agenttask",
            name="ask_closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(forward_tasks, migrations.RunPython.noop),
        migrations.RenameField(model_name="agenttask", old_name="dispatch", new_name="on_approve"),
        migrations.RemoveField(model_name="agenttask", name="uuid"),
        migrations.RemoveField(model_name="agenttask", name="decision"),
        migrations.RemoveField(model_name="agenttask", name="comment"),
        migrations.RemoveField(model_name="agenttask", name="decided_by"),
        migrations.RemoveField(model_name="agenttask", name="decided_by_user"),
        migrations.RemoveField(model_name="agenttask", name="decided_at"),
        migrations.RemoveField(model_name="agenttask", name="ask_dismissed"),
        # (5) AgentTaskCommand -> AgentTaskAction.
        migrations.RenameModel(old_name="AgentTaskCommand", new_name="AgentTaskAction"),
        migrations.RenameField(model_name="agenttaskaction", old_name="kind", new_name="action"),
        migrations.RenameField(model_name="agenttaskaction", old_name="created_by", new_name="by"),
        migrations.AddField(
            model_name="agenttaskaction",
            name="comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="agenttaskaction",
            name="by_user",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="task_actions", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(forward_actions, migrations.RunPython.noop),
        migrations.RemoveField(model_name="agenttaskaction", name="payload"),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="task",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="actions",
                to="agents.agenttask",
            ),
        ),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="agent",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="task_actions",
                to="agents.agent",
            ),
        ),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="action",
            field=models.CharField(
                choices=[("approve", "approve"), ("decline", "decline"), ("reply", "reply"),
                         ("dispatch", "dispatch"), ("done", "done")],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="by",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="agenttaskaction",
            name="status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("applied", "Applied")],
                default="applied", max_length=10,
            ),
        ),
        migrations.AlterModelOptions(
            name="agenttaskaction",
            options={"ordering": ["-created_at", "-id"]},
        ),
        # The (agent, status) index is auto-named from the table, which just changed.
        migrations.RenameIndex(
            model_name="agenttaskaction",
            new_name="agents_agen_agent_i_eca49c_idx",
            old_name="agents_agen_agent_i_6970c7_idx",
        ),
        # (6) Work products are gone.
        migrations.DeleteModel(name="AgentWorkProduct"),
    ]
