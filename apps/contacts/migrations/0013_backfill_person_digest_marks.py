"""Seed `PersonDigestMark` from the v1 digests (people digest v2, canopy#820).

Its own migration, AFTER the schema one, on purpose: Postgres creates 0012's
foreign-key indexes from deferred SQL at the END of that migration, and rows
written by a RunPython in the same migration leave "pending trigger events"
that make the CREATE INDEX fail (see 0011; SQLite cannot show it).

A v1 digest an agent wrote is that agent's last digest of that person, so its
`updated_at` is the pair's watermark. Without this, the first v2 sweep would
re-read every such person's last 14 days. Idempotent: a pair that already has a
mark keeps it.
"""
from django.db import migrations


def seed_marks(apps, schema_editor):
    PersonDigest = apps.get_model("contacts", "PersonDigest")
    PersonDigestMark = apps.get_model("contacts", "PersonDigestMark")
    for digest in PersonDigest.objects.filter(updated_by_agent__isnull=False).iterator():
        PersonDigestMark.objects.get_or_create(
            person_id=digest.person_id, agent_id=digest.updated_by_agent_id,
            defaults={"digested_at": digest.updated_at})


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0012_people_digest_v2"),
    ]

    operations = [
        migrations.RunPython(seed_marks, migrations.RunPython.noop),
    ]
