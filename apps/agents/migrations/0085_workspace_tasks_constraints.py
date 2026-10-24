"""The workspace-task constraints, once every row has its workspace, number,
key and handle (0084); and the columns they replace go."""
import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agents", "0084_workspace_tasks_data")]

    operations = [
        migrations.AlterField(
            model_name="agentproject",
            name="workspace",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="projects",
                                    to="workspaces.workspace"),
        ),
        migrations.AlterField(
            model_name="agenttask",
            name="workspace",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks",
                                    to="workspaces.workspace"),
        ),
        migrations.AlterField(
            model_name="agenttask",
            name="number",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="agenttask",
            name="key",
            field=models.CharField(max_length=24, unique=True),
        ),
        migrations.AddConstraint(
            model_name="agentproject",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ext_id", ""), _negated=True),
                fields=("created_by_agent", "ext_id"),
                name="project_agent_alias_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="agentproject",
            constraint=models.UniqueConstraint(
                fields=("workspace", "handle"), name="project_handle_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="agentproject",
            constraint=models.CheckConstraint(
                condition=models.Q(("handle", ""), _negated=True),
                name="project_has_handle",
            ),
        ),
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("ext_id"),
                models.F("created_by_agent"),
                condition=models.Q(("ext_id", ""), _negated=True),
                name="uniq_task_agent_alias_ci",
            ),
        ),
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.UniqueConstraint(
                fields=("workspace", "number"), name="uniq_task_number"
            ),
        ),
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("agent__isnull", True),
                    ("assignee_user__isnull", True),
                    _connector="OR",
                ),
                name="task_one_assignee",
            ),
        ),
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.CheckConstraint(
                condition=models.Q(("confidence__in", ["", "high", "medium", "low"])),
                name="task_confidence_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.CheckConstraint(
                condition=models.Q(("priority__lte", 4)), name="task_priority_known"
            ),
        ),
        migrations.RemoveField(
            model_name="agentproject",
            name="owner_workspace",
        ),
        migrations.RemoveField(
            model_name="agentproject",
            name="scope",
        ),
        migrations.RemoveField(
            model_name="agenttask",
            name="position",
        ),
    ]
