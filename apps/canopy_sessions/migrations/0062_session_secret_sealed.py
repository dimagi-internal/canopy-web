from django.db import migrations, models


class Migration(migrations.Migration):
    """A chat secret is sealed in the sharer's browser; canopy-web keeps no value it
    can read. The Fernet column goes with every value in it: rows live 30 minutes,
    and one left over has an empty `value_zk`, which every read treats as absent."""

    dependencies = [
        ("canopy_sessions", "0061_handoff_cli"),
    ]

    operations = [
        migrations.RemoveField(model_name="sessionsecret", name="value_enc"),
        migrations.AddField(
            model_name="sessionsecret",
            name="value_zk",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
    ]
