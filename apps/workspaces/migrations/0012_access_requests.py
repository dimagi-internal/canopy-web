# Self-join becomes "request an invitation" (owner decision, 2026-10-04).
#
# * `self_join_domains` -> `access_request_domains`: RenameField, so the values
#   on `dimagi` survive exactly as they are. The domains keep their people; what
#   they now grant is the right to ASK, not to join.
# * `auto_approve_role`: blank (off) on every workspace except `dimagi`, which
#   is set to `editor` while it bootstraps (Jonathan, 2026-10-04: "for now",
#   to be turned off later — a settings change, not code). Capped at editor.
# * `WorkspaceAccessRequest`: one open (pending) request per (workspace, user).
# * `WorkspaceInvite.role` defaults to viewer. Existing invites keep the role
#   they stored — the column has always been NOT NULL, so every one has one.
#
# Existing memberships are untouched.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DIMAGI = "dimagi"


def dimagi_auto_approves_as_editor(apps, schema_editor):
    """Only `dimagi`; every other workspace stays off. Its domain list is
    untouched (the RenameField above carried it over as it was)."""
    Workspace = apps.get_model("workspaces", "Workspace")
    Workspace.objects.filter(slug=DIMAGI).update(auto_approve_role="editor")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0011_admin_role"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="workspace",
            old_name="self_join_domains",
            new_name="access_request_domains",
        ),
        migrations.AlterField(
            model_name="workspace",
            name="access_request_domains",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Email domains (lowercased, no leading '@') whose users may REQUEST "
                    "an invitation to this workspace. A request grants nothing until approved."
                ),
            ),
        ),
        migrations.AddField(
            model_name="workspace",
            name="auto_approve_role",
            field=models.CharField(
                blank=True, default="", max_length=16,
                choices=[("", "Off"), ("viewer", "Viewer"), ("editor", "Editor")],
            ),
        ),
        migrations.AlterField(
            model_name="workspaceinvite",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("admin", "Admin"), ("editor", "Editor"), ("viewer", "Viewer")],
                default="viewer",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="WorkspaceAccessRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("note", models.TextField(blank=True, default="")),
                ("status", models.CharField(
                    choices=[("pending", "Pending"), ("approved", "Approved"), ("denied", "Denied")],
                    default="pending", max_length=16,
                )),
                ("role", models.CharField(
                    blank=True,
                    choices=[("owner", "Owner"), ("admin", "Admin"), ("editor", "Editor"), ("viewer", "Viewer")],
                    default="", max_length=16,
                )),
                ("auto", models.BooleanField(default=False)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_reason", models.TextField(blank=True, default="")),
                ("notify_result", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("decided_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="workspace_access_requests", to=settings.AUTH_USER_MODEL,
                )),
                ("workspace", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="access_requests", to="workspaces.workspace",
                )),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["workspace", "status", "-created_at"],
                                         name="ws_access_req_status_idx")],
                "constraints": [models.UniqueConstraint(
                    condition=models.Q(("status", "pending")),
                    fields=("workspace", "user"),
                    name="uniq_ws_access_request_open",
                )],
            },
        ),
        migrations.RunPython(dimagi_auto_approves_as_editor, noop),
    ]
