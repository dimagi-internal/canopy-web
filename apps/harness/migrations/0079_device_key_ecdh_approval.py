"""A device key's ECDH key gets its own approval (Canopy Vault, canopy-web#1550).

Until now a new ECDH key sent an ACTIVE key back to `pending`, which also cut
the box's sign-in (runner sessions need an active key) — on 2026-10-18 that
took cloud-ec2-2 offline for every agent. Now the signing key stays active and
only the ECDH key waits.

Data:
- an active key with an ECDH key: that ECDH key was approved with it.
- a pending key that HAD been approved (`approved_at` set, never revoked) was
  sent back only by a new ECDH key: its signing key returns to active, its
  ECDH key waits for approval.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def forwards(apps, schema_editor):
    key_model = apps.get_model("harness", "RunnerDeviceKey")
    for k in key_model.objects.filter(status="active").exclude(ecdh_jkt=""):
        k.ecdh_approved_at = k.approved_at or timezone.now()
        k.ecdh_approved_by_id = k.approved_by_id
        k.save(update_fields=["ecdh_approved_at", "ecdh_approved_by"])
    key_model.objects.filter(status="pending", approved_at__isnull=False,
                       revoked_at__isnull=True).update(status="active")


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0078_runner_access_token"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="runnerdevicekey",
            name="ecdh_approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="runnerdevicekey",
            name="ecdh_approved_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
