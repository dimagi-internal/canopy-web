"""`Message.sealed` holds ciphertext, not JSON: a sensitive conversation's words are
unreadable to a database read, a backup or a shell (harness.sensitivity)."""
import json

from django.db import migrations, models


def encrypt_existing(apps, schema_editor):
    from apps.common.encryption import encrypt_secret

    Message = apps.get_model("canopy_sessions", "Message")
    for m in Message.objects.filter(sealed__isnull=False).only("pk", "sealed").iterator():
        Message.objects.filter(pk=m.pk).update(
            sealed_ct=encrypt_secret(json.dumps(m.sealed, sort_keys=True)))


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0047_message_sealed"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="sealed_ct",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(encrypt_existing, migrations.RunPython.noop),
        migrations.RemoveField(model_name="message", name="sealed"),
        migrations.RenameField(model_name="message", old_name="sealed_ct", new_name="sealed"),
    ]
