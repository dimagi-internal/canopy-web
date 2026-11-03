"""Rewrite the stored reason of the seeded `offered-nothing` rule to the access-list wording.

Only a row whose reason is EXACTLY a retired seeded text changes; one a person edited is left
alone. Its own migration: it updates rows other tables reference (CLAUDE.md, migrations)."""
from django.db import migrations

OLD = (
    "the agent's interface offers this person nothing: they are not one of its "
    "admins, and no `full:` rule or capability lists them.",
    "the agent offers this person nothing: they are not one of its "
    "admins, and nothing on its access list lets them in.",
)
NEW = ("the agent's access list gives this person nothing: they are not one of its admins "
       "and no entry on its access list names them.")


def rewrite(apps, schema_editor):
    apps.get_model("admission", "AdmissionRule").objects.filter(
        seed_key="offered-nothing", reason__in=OLD).update(reason=NEW)


class Migration(migrations.Migration):
    dependencies = [("admission", "0010_origin_local")]

    operations = [migrations.RunPython(rewrite, migrations.RunPython.noop)]
