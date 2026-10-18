"""Turn facts recorded before they named their runner get it from the turn, while
the turn is still held (a purged turn's fact stays as it was)."""
import uuid

from django.db import migrations


def _is_uuid(ref: str) -> bool:
    try:
        uuid.UUID(ref)
    except ValueError:
        return False
    return True


def name_runners(apps, schema_editor):
    Activity = apps.get_model("events", "Activity")
    Turn = apps.get_model("harness", "Turn")
    facts = Activity.objects.filter(kind="turn.finished").exclude(meta__has_key="runner")
    refs = {f.ref for f in facts.only("ref") if _is_uuid(f.ref)}
    boxes = {str(pk): (name, kind) for pk, name, kind in Turn.objects.filter(
        pk__in=refs, claimed_by__isnull=False).values_list("pk", "claimed_by__name", "claimed_by__kind")}
    for fact in facts.filter(ref__in=boxes):
        name, kind = boxes[fact.ref]
        fact.meta = {**(fact.meta or {}), "runner": name, "runner_kind": kind}
        fact.save(update_fields=["meta"])


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0005_box_usage_report"),
        ("harness", "0081_box_usage_report"),
    ]

    operations = [migrations.RunPython(name_runners, migrations.RunPython.noop)]
