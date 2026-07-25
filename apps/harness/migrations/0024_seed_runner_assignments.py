from django.db import migrations


def seed(apps, schema_editor):
    # Calls the live service helper deliberately: the logic reads JSON fields and
    # sorts — historical models add nothing here, and the helper is idempotent.
    from apps.harness.services import seed_assignments_from_capabilities
    seed_assignments_from_capabilities()


class Migration(migrations.Migration):
    dependencies = [("harness", "0023_turn_pinned_runner_alter_item_origin_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
