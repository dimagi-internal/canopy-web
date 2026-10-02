"""Rename TeleportRequest -> TransferRequest.

A move between runners is one operation with one name: `transfer`. A transfer onto
someone else's box waits for their approval as a transfer REQUEST; "teleport"
was a second name for the same thing and is gone.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0037_teleport_request"),
        ("harness", "__first__"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="teleportrequest",
            name="one_pending_teleport_per_session",
        ),
        migrations.RenameModel(old_name="TeleportRequest", new_name="TransferRequest"),
        migrations.AlterField(
            model_name="transferrequest",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="transfer_requests",
                to="canopy_sessions.session",
            ),
        ),
        migrations.AlterField(
            model_name="transferrequest",
            name="to_runner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="transfer_requests",
                to="harness.runner",
            ),
        ),
        migrations.AddConstraint(
            model_name="transferrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("session",),
                name="one_pending_transfer_request_per_session",
            ),
        ),
    ]
