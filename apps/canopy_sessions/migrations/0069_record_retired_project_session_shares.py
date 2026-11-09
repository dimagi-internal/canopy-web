"""Record every project session share still live when the feature was retired.

A `ProjectSessionShare` gave a project's team (its owner and its participants) editor
rights and pull over the sharer's laptop sessions in the project's repo. Nobody could
start one any more, and the shares that remained are ended by the migration after this
one, which drops the table. Each live row is first written to the settings audit
(`SettingChange`, operation `project_session_share_retired`), ids only, so the people
who lost that access are traceable. With no live row this writes nothing.

Its own migration, ahead of the schema change: it reads rows other tables reference.
"""
from django.db import migrations


def record_live_shares(apps, schema_editor):
    Share = apps.get_model("canopy_sessions", "ProjectSessionShare")
    Change = apps.get_model("events", "SettingChange")
    for share in Share.objects.filter(revoked_at__isnull=True).select_related("project").order_by("pk"):
        target = f"user:{share.user_id} / project:{share.project_id}"
        Change.objects.create(
            workspace_id=share.project.workspace_id, actor=None, actor_email="", via="system",
            operation="project_session_share_retired", method="MIGRATE",
            path="canopy_sessions/0069", target=target,
            summary=f"{target}: the share of laptop sessions with the project team ended "
                    "because the feature was retired"[:500],
            changes=[{"scope": "session-sharing", "changed": True,
                      "before": {"user_id": share.user_id, "project_id": share.project_id,
                                 "shared_since": share.created_at.isoformat(), "shared": True},
                      "after": {"user_id": share.user_id, "project_id": share.project_id, "shared": False,
                                "reason": "feature retired"}}])


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0068_session_relation"),
        ("events", "0009_session_usage_workspace_required"),
    ]
    operations = [migrations.RunPython(record_live_shares, migrations.RunPython.noop)]
