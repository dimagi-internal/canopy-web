import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SeenJti",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=200, unique=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"db_table": "canopy_host_seen_jti"},
        ),
        migrations.CreateModel(
            name="DelegatedToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_checksum", models.CharField(max_length=64, unique=True)),
                ("subject", models.CharField(db_index=True, max_length=255)),
                ("client_id", models.CharField(max_length=255)),
                ("actor", models.CharField(blank=True, max_length=255)),
                ("scope", models.CharField(max_length=255)),
                ("cnf_jkt", models.CharField(max_length=64)),
                ("grant_jti", models.CharField(blank=True, max_length=64)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "canopy_host_delegated_token"},
        ),
    ]
