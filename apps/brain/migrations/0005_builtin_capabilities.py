"""The one capability canopy itself needs to exist: `project-readers`, the purpose a person reading a
project's sessions as themselves is asking under (`access._brain_project_q`). Features that learn are
registered by a canopy admin (`registry.py`), never seeded. Idempotent."""
from django.db import migrations


def seed(apps, schema_editor):
    BrainCapability = apps.get_model("brain", "BrainCapability")
    BrainCapability.objects.get_or_create(slug="project-readers", defaults={
        "name": "Project readers",
        "description": "A project's own readers read the sessions tied to it (read-only, redacted).",
        "purpose_class": "project", "subject_type": "project", "purposes": ["read"],
        "source_kinds": ["session"], "output_kinds": [], "audience": ["project_readers"], "enabled": True})


class Migration(migrations.Migration):
    dependencies = [("brain", "0004_outputs_and_reporting")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
