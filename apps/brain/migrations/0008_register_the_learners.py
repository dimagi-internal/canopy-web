"""Register, dark, what canopy's learning is today and will be: so the list a person agrees to is complete.

Each starts `enabled` false with no operator: registering one changes nothing, and coaching keeps reading
`CoachingOptIn` until its reads move. A canopy admin names an operator and switches one on (`registry.py`)."""
from django.db import migrations

LEARNERS = [
    ("coaching", "Individual coaching", "individual", ["coach", "review"], ["coaching_note", "pattern", "practice"],
     "Coaches learn how you work and suggest how to work better."),
    ("agent-memory", "Personal memory", "memory", ["remember"], ["hcp_entry"],
     "Agents remember what you tell them about yourself and your work."),
    ("project-link-judge", "Project links", "project", ["judge"], ["link"],
     "Decides which project a session belongs to when its activity alone does not say."),
    ("project-learning", "Project learning", "project", ["learn"],
     ["idea", "task", "update_draft", "contributor_page", "risk"],
     "Your sessions on a project help it move forward; its team can read them."),
    ("system-learning", "System learning", "system", ["improve"], ["finding"],
     "Canopy learns where its agents and tools fail, to fix them."),
]
AUDIENCE = {"individual": ["person", "coaches", "manager"], "memory": ["serving_agents"],
            "project": ["project_readers"], "system": ["canopy_admins", "agent_admins"]}
SUBJECT = {"individual": "person", "memory": "person", "project": "project", "system": "system"}


def register(apps, schema_editor):
    BrainCapability = apps.get_model("brain", "BrainCapability")
    for slug, name, cls, purposes, outputs, description in LEARNERS:
        BrainCapability.objects.get_or_create(slug=slug, defaults={
            "name": name, "description": description, "purpose_class": cls, "subject_type": SUBJECT[cls],
            "purposes": purposes, "source_kinds": ["session"], "output_kinds": outputs, "audience": AUDIENCE[cls],
            "enabled": False})


class Migration(migrations.Migration):
    dependencies = [("brain", "0006_runs")]
    operations = [migrations.RunPython(register, migrations.RunPython.noop)]
