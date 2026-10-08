"""Existing facts filed against a project make their subjects participants
(fleet brain v1.1, canopy#804).

Separate from 0040 for the reason in `contacts/0011`: on Postgres, 0040's
deferred index SQL runs at the end of that migration and fails on rows a
RunPython in the same migration just wrote ("pending trigger events").
"""
from django.db import migrations


def link_fact_subjects(apps, schema_editor):
    """Every existing fact filed against a project makes its subject a
    participant (source=fact). Idempotent. The task-raiser half needs the live
    person resolution and is `manage.py backfill_project_participants`."""
    PersonFact = apps.get_model("contacts", "PersonFact")
    ProjectParticipant = apps.get_model("agents", "ProjectParticipant")
    pairs = (PersonFact.objects.filter(project__isnull=False)
             .values_list("project_id", "person_id").distinct())
    for project_id, person_id in pairs:
        ProjectParticipant.objects.get_or_create(
            project_id=project_id, person_id=person_id, defaults={"source": "fact"})


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0040_project_participants_and_digest_switch"),
        ("contacts", "0011_mirror_contact_notes"),
    ]

    operations = [
        migrations.RunPython(link_fact_subjects, migrations.RunPython.noop),
    ]
