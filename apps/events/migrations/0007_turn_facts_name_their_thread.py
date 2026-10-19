"""Turn facts recorded before they named their thread, contact and asking agent get
them from the turn, while the turn is still held (`activity.who_and_where`; a
purged turn keeps its row, so nearly every fact can be filled)."""
import uuid

from django.db import migrations

BATCH = 2000


def _is_uuid(ref: str) -> bool:
    try:
        uuid.UUID(ref)
    except ValueError:
        return False
    return True


def name_threads(apps, schema_editor):
    Activity = apps.get_model("events", "Activity")
    Turn = apps.get_model("harness", "Turn")
    Agent = apps.get_model("agents", "Agent")
    agent_of_login = dict(Agent.objects.filter(user__isnull=False).values_list("user_id", "slug"))
    facts = (Activity.objects.filter(kind="turn.enqueued").exclude(meta__has_key="thread")
             .exclude(meta__has_key="contact").exclude(meta__has_key="by_agent").order_by("pk"))
    last = 0
    while True:
        chunk = list(facts.filter(pk__gt=last)[:BATCH])
        if not chunk:
            return
        last = chunk[-1].pk
        refs = [f.ref for f in chunk if _is_uuid(f.ref)]
        turns = {str(t["pk"]): t for t in Turn.objects.filter(pk__in=refs).values(
            "pk", "chat_session_id", "origin", "origin_ref", "initiator_contact_id", "initiator_agent")}
        changed = []
        for fact in chunk:
            t = turns.get(fact.ref)
            if t is None:
                continue
            extra = {}
            ref = t["origin_ref"] if isinstance(t["origin_ref"], dict) else {}
            if t["chat_session_id"]:
                extra["thread"] = f"session:{t['chat_session_id']}"
            elif t["origin"] == "email" and ref.get("thread_id"):
                extra["thread"] = f"mail:{ref['thread_id']}"
            if t["initiator_contact_id"]:
                extra["contact"] = t["initiator_contact_id"]
            by_agent = t["initiator_agent"] or (
                agent_of_login.get(fact.actor_id, "") if fact.actor_kind == "agent" else "")
            if by_agent:
                extra["by_agent"] = by_agent
            if extra:
                fact.meta = {**(fact.meta or {}), **extra}
                changed.append(fact)
        Activity.objects.bulk_update(changed, ["meta"])


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0006_turn_facts_name_their_runner"),
        ("harness", "0083_runner_app_preference"),
        ("agents", "0063_project_mcp"),
    ]

    operations = [migrations.RunPython(name_threads, migrations.RunPython.noop)]
