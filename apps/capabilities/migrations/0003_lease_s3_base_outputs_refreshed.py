from django.db import migrations, models


def legacy_base(apps, schema_editor):
    """Every lease before this migration lives under leases/<id>/ (the pre-0.9.0
    layout); record that, so a stack updated later never moves its objects."""
    Lease = apps.get_model("capabilities", "CapabilityLease")
    for pk in Lease.objects.filter(s3_base="").values_list("pk", flat=True).iterator():
        Lease.objects.filter(pk=pk).update(s3_base=f"leases/{pk}")


class Migration(migrations.Migration):
    dependencies = [
        ("capabilities", "0002_lease_execs"),
    ]

    operations = [
        migrations.AddField(
            model_name="capabilitylease",
            name="s3_base",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
        migrations.AddField(
            model_name="agentcapability",
            name="outputs_refreshed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="capabilitylease",
            index=models.Index(fields=["workspace", "-created_at"], name="cap_lease_ws_created_idx"),
        ),
        migrations.RunPython(legacy_base, migrations.RunPython.noop),
    ]
