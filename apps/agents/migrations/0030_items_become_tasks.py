"""Every `Item` becomes a task carrying an ask.

The two stopped being different things, so the rows move rather than the models
staying side by side. Two properties this migration has to keep:

* **Ids survive.** A task's `uuid` is set to the item's own id, so
  `/api/items/{id}/`, an Ada-stored reference and any link a human sent still
  resolve. That is the entire reason tasks grew a uuid.
* **The cycle survives.** `Turn.raised_from` pointed at the item that dispatched
  it; those turns are re-pointed at the task via `raised_from_task`, or the
  "what approved this work" edge would be lost the moment the item table goes.

Reversible: the tasks it created are deleted again (identified by carrying an
item's uuid), which leaves the untouched `Item` rows exactly as they were.
"""

from django.db import migrations


def _status(item, AgentTask):
    """Where a migrated ask sits on the board.

    An item had three states; a board column says something slightly different,
    so the mapping is explicit rather than clever:
      open            -> suggested   (the agent proposed it, a human validates)
      dismissed       -> declined
      decided+skip    -> declined
      decided+defer   -> suggested   (not now is not never — the card stays)
      implement/answer-> in_progress if it dispatched work, else suggested
    """
    if item.state == "open":
        return "suggested"
    if item.state == "dismissed" or item.decision == "skip":
        return "declined"
    if item.decision == "defer":
        return "suggested"
    return "in_progress" if item.dispatched_at else "suggested"


def items_to_tasks(apps, schema_editor):
    Item = apps.get_model("harness", "Item")
    AgentTask = apps.get_model("agents", "AgentTask")
    Agent = apps.get_model("agents", "Agent")
    Turn = apps.get_model("harness", "Turn")

    seq = {}  # agent_id -> the T<N> counter, read once and advanced in memory
    for item in Item.objects.all().order_by("created_at").iterator():
        if AgentTask.objects.filter(uuid=item.id).exists():
            continue  # re-run safe
        if item.agent_id not in seq:
            agent = Agent.objects.filter(pk=item.agent_id).first()
            seq[item.agent_id] = (agent.task_seq if agent else 0)
        seq[item.agent_id] += 1

        task = AgentTask.objects.create(
            agent_id=item.agent_id,
            ext_id=f"T{seq[item.agent_id]}",
            uuid=item.id,
            title=item.title,
            status=_status(item, AgentTask),
            ask_kind=item.kind,
            ask_body=item.body,
            ask_dismissed=(item.state == "dismissed"),
            decision=item.decision,
            comment=item.comment,
            decided_by=item.decided_by,
            decided_by_user_id=item.decided_by_user_id,
            decided_at=item.decided_at,
            dispatch=item.dispatch,
            dispatched_at=item.dispatched_at,
            batch_key=item.batch_key,
            idempotency_key=item.idempotency_key,
            origin=item.origin,
            origin_ref=item.origin_ref,
            raised_by_id=item.raised_by_id,
            source="item",
        )
        # `created_at` is auto_now_add, so it has to be written after the fact —
        # an inbox sorted by age would otherwise show every migrated ask as new.
        AgentTask.objects.filter(pk=task.pk).update(created_at=item.created_at)
        Turn.objects.filter(raised_from_id=item.id).update(raised_from_task_id=task.pk)

    # Leave each agent's counter where the migrated tasks left it, or the next
    # task the agent creates would collide with one of these ext_ids.
    for agent_id, value in seq.items():
        Agent.objects.filter(pk=agent_id, task_seq__lt=value).update(task_seq=value)


def tasks_back_to_items(apps, schema_editor):
    """Drop the tasks this created. The `Item` rows were never touched, so the
    old world is intact; anything written against the new tasks since is not,
    which is what reversing a data migration means."""
    Item = apps.get_model("harness", "Item")
    AgentTask = apps.get_model("agents", "AgentTask")
    ids = list(Item.objects.values_list("id", flat=True))
    AgentTask.objects.filter(uuid__in=ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0029_task_ask_dismissed"),
        ("harness", "0044_turn_raised_from_task"),
    ]

    operations = [
        migrations.RunPython(items_to_tasks, tasks_back_to_items),
    ]
