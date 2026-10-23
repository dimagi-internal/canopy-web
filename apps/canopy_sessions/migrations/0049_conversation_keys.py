"""A sensitive conversation's key pair (public half) and each person's wrap of its
private half; then every row still sealed under the field key is re-sealed to its
conversation's new key, wrapped to its starter (spec
2026-10-23-sensitive-conversations-sealed-to-their-people §2, "Existing rows").

A session whose starter has no vault keyset keeps the field key and is logged as
needing migration: `manage.py reseal_sensitive_conversations` moves it once they have
one. Until then its words are not served to anyone (`sensitivity.readable` hands out
only `cc1:` envelopes) — canopy-web no longer opens them to show them."""
import logging

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def reseal(apps, schema_editor):
    from apps.canopy_sessions.conversation_keys import reseal_legacy

    done = reseal_legacy(
        Session=apps.get_model("canopy_sessions", "Session"),
        Message=apps.get_model("canopy_sessions", "Message"),
        SessionKey=apps.get_model("canopy_sessions", "SessionKey"),
        SessionKeyWrap=apps.get_model("canopy_sessions", "SessionKeyWrap"),
        UserVaultKeyset=apps.get_model("passkeys", "UserVaultKeyset"),
        log_to=logging.getLogger("canopy_sessions.migrations.0049"),
    )
    if done["pending"]:
        logging.getLogger("canopy_sessions.migrations.0049").warning(
            "%d sensitive session(s) still need migration (starter has no vault key): %s",
            len(done["pending"]), ", ".join(done["pending"]))


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
