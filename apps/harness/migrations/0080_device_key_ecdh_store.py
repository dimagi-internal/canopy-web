"""Where a device key's ECDH private key lives, as the box reports it — shown on
Settings > Your machines ("key locked in the security chip", "key held by AWS").
Display only; nothing trusts it."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0079_device_key_ecdh_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="runnerdevicekey",
            name="ecdh_store",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
    ]
