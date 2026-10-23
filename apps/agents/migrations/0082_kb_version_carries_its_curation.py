"""A knowledge-base version carries the curation state it was built from.

Before: one pointer per project (`AgentProject.kb_curation`), saved by compare-and-swap
under a lease, beside the version log. After: each version names its own state
(`KbVersion.curation_sha`), a refresh starts from the head's, and a rollback rolls the
state back with the file. The project's pointer moves onto its head version; the
records state (`state_sha`) and the server-side change log (`KbChange`) go: what changed
is the file's own history.
"""
from django.db import migrations, models


def head_takes_the_pointer(apps, schema_editor):
    AgentProject = apps.get_model("agents", "AgentProject")
    KbVersion = apps.get_model("agents", "KbVersion")
    for p in AgentProject.objects.exclude(kb_version=None).only("id", "kb_version_id", "kb_curation"):
        sha = (p.kb_curation or {}).get("sha") or ""
        if sha:
            KbVersion.objects.filter(pk=p.kb_version_id).update(curation_sha=sha)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0081_vault_roles_owner_admin_member"),
    ]

    operations = [
        migrations.AddField(
            model_name="kbversion",
            name="curation_sha",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(head_takes_the_pointer, migrations.RunPython.noop),
        migrations.RemoveField(model_name="agentproject", name="kb_curation"),
        migrations.RemoveField(model_name="kbversion", name="state_sha"),
        migrations.DeleteModel(name="KbChange"),
    ]
