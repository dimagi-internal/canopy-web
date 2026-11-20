"""Entries the person wrote themself are `authenticated` (HCP v2 3.6); every other stays `unverified`.

Its own migration, after the column was added: a data change never shares one with a schema change
(CLAUDE.md, Migrations)."""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    PersonFact = apps.get_model("contacts", "PersonFact")
    PersonFact.objects.filter(
        asserted_by_agent__isnull=True, asserted_by_user__isnull=False,
        asserted_by_user_id=F("person__user_id")).update(assurance="authenticated")


class Migration(migrations.Migration):
    dependencies = [("contacts", "0030_hcp_v2_entry_fields")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
