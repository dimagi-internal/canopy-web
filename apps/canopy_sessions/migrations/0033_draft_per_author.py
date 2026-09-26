# Per-author drafts (spec 2026-09-26): one open draft per (session, author)
# replaces the single shared draft + soft lock.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _drop_shared_drafts(apps, schema_editor):
    # A shared draft has no author to backfill; losing in-progress text once is
    # acceptable. Runs FIRST so no row ever takes the placeholder default below.
    apps.get_model("canopy_sessions", "Draft").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0032_message_author"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(_drop_shared_drafts, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="draft",
            name="one_open_draft_per_session",
        ),
        migrations.RemoveField(
            model_name="draft",
            name="last_editor",
        ),
        migrations.AddField(
            model_name="draft",
            name="author",
            field=models.ForeignKey(
                default=1,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="draft",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slot", "next")),
                fields=("session", "author"),
                name="one_open_draft_per_session_author",
            ),
        ),
    ]
