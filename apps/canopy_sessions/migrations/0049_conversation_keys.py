"""A sensitive conversation's key pair (public half) and each person's wrap of its
private half (spec 2026-10-23-sensitive-conversations-sealed-to-their-people §2)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def reseal(apps, schema_editor):
    # This re-sealed every row still under the field key to its conversation's own key.
    # Every row was re-sealed in production, and the function that did it is gone; the
    # operation stays so the migration graph is unchanged.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0048_message_sealed_encrypted"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("passkeys", "0003_vault_keyset_replaced_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="SessionKey",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("conversation", models.UUIDField()),
                ("public_jwk", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "session",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_key",
                        to="canopy_sessions.session",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SessionKeyWrap",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("wrap", models.JSONField()),
                ("recipient_jkt", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="key_wraps",
                        to="canopy_sessions.session",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("session", "user"), name="one_key_wrap_per_session_user"
                    )
                ],
            },
        ),
        migrations.RunPython(reseal, migrations.RunPython.noop),
    ]
