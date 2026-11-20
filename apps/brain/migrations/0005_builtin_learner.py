"""The one learner canopy itself needs: `project-readers`, the purpose a person reading a project's sessions as
themselves is asking under (`readers.project_q`). Its own migration, ahead of any later schema change to the table
(a row written and an index created in one PostgreSQL transaction fail with "pending trigger events").
`registry.PROJECT_READERS` says the same thing for a database built without migrations and
`tests/test_brain_admin_api.py` holds the two equal. Features that learn are registered by a canopy admin
(`registry.py`), never seeded here. Idempotent."""
from django.db import migrations


def seed(apps, schema_editor):
    BrainCapability = apps.get_model("brain", "BrainCapability")
    BrainCapability.objects.get_or_create(slug="project-readers", defaults={
        "name": "Project readers",
        "description": "A project's own readers read the sessions tied to it (read-only, redacted).",
        "purpose_class": "project", "subject_type": "project", "purposes": ["read"],
        "source_kinds": ["session"], "output_kinds": [], "audience": ["project_readers"], "enabled": True})


class Migration(migrations.Migration):
    dependencies = [("brain", "0004_outputs_runs_and_reporting")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
