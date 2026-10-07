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

`ext_id` becomes unique per agent ignoring case, and loses '/': a later row
that collides with an older one (in any case) is renamed `<id>-2`, `-3` …
(`safe_ext_ids`).

Not reversible in practice: the data steps reverse as no-ops, and re-adding the
unique `uuid` column to a table with more than one row would fail exactly as
0027 once did. Restore from backup rather than migrating back past this.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models.functions import Lower

ACTION_FOR_KIND = {"accept": "approve", "decline": "decline", "comment": "reply",
                   "dispatch": "dispatch", "done": "done"}


def comment_from_payload(payload: dict) -> str:
    return str((payload or {}).get("note") or (payload or {}).get("reason") or "")


EXT_ID_MAX = 64


def _suffixed(base: str, n: int) -> str:
    suffix = f"-{n}"
    return base[: EXT_ID_MAX - len(suffix)] + suffix


def safe_ext_ids(rows) -> dict:
    """The ext_ids that must change so one agent's ids are unique ignoring case
    and carry no '/' (a task is addressed as `/tasks/{ext_id}/`).

    `rows` is `(pk, agent_id, ext_id)` OLDEST FIRST. Returns `{pk: new_ext_id}`
    for the rows that change. '/' becomes '-'. Within an agent the oldest row
    holding an id (case-insensitively) keeps it; each later one becomes
    `<id>-2`, `<id>-3` … — the first suffix no row keeps or was already given.
    Ids that need no rename are reserved FIRST, so a rename never takes the id a
    later, untouched row already has.
    """
    by_agent: dict = {}
    for pk, agent_id, ext_id in rows:
        by_agent.setdefault(agent_id, []).append((pk, ext_id))
    renames = {}
    for agent_rows in by_agent.values():
        taken: set = set()
        keep = set()
        for pk, ext_id in agent_rows:
            wanted = ext_id.replace("/", "-")
            if wanted.lower() not in taken:
                taken.add(wanted.lower())
                keep.add(pk)
        for pk, ext_id in agent_rows:
            wanted = ext_id.replace("/", "-")
            if pk not in keep:
                n = 2
                while _suffixed(wanted, n).lower() in taken:
                    n += 1
                wanted = _suffixed(wanted, n)
                taken.add(wanted.lower())
            if wanted != ext_id:
                renames[pk] = wanted
    return renames


def forward_tasks(apps, schema_editor):
    Task = apps.get_model("agents", "AgentTask")  # noqa: N806
    rows = Task.objects.order_by("created_at", "id").values_list("pk", "agent_id", "ext_id")
    renames = safe_ext_ids(list(rows))
    # One write per row, both changes at once — see forward_actions on why a
    # second UPDATE of a row in this transaction breaks the ALTERs that follow.
    todo = Task.objects.filter(models.Q(decided_at__isnull=False) | models.Q(pk__in=list(renames)))
    for row in todo.iterator():
        fields = []
        if row.decided_at is not None:
            row.ask_closed_at = row.decided_at
            fields.append("ask_closed_at")
        if row.pk in renames:
            row.ext_id = renames[row.pk]
            fields.append("ext_id")
        row.save(update_fields=fields)
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def forward_actions(apps, schema_editor):
    Action = apps.get_model("agents", "AgentTaskAction")  # noqa: N806
    Action.objects.filter(task__isnull=True).delete()
    Action.objects.exclude(action__in=list(ACTION_FOR_KIND)).delete()
    # One write per row. A second UPDATE of the same row in this transaction makes
    # Postgres queue deferred FK checks, and the ALTERs that follow would then
    # fail with "pending trigger events".
    for row in Action.objects.all().iterator():
        row.action = ACTION_FOR_KIND[row.action]
        row.comment = comment_from_payload(row.payload)
        if row.status == "dismissed":
            row.status = "applied"
        row.save(update_fields=["action", "comment", "status"])
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


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
        # The case-sensitive unique goes BEFORE the renames: forward_tasks saves
        # row by row, so an older 'a/b' becomes 'a-b' while a newer 'a-b' still
        # holds that id (it is renamed 'a-b-2' a save later). The case-insensitive
        # constraint that replaces it is added at the end (7), once the data fits.
        migrations.RemoveConstraint(model_name="agenttask", name="uniq_agent_task_extid"),
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
        # (7) ext_id unique per agent IGNORING CASE — lookups are iexact, so "t1"
        # beside "T1" would leave one unreachable. forward_tasks made the data fit.
        migrations.AddConstraint(
            model_name="agenttask",
            constraint=models.UniqueConstraint(
                Lower("ext_id"), "agent", name="uniq_agent_task_extid_ci",
            ),
        ),
    ]
