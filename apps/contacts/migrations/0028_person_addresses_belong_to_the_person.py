# Contact details belong to the person's account, not to a workspace: one set
# per person, changed only by them.

from django.db import migrations, models


def keep_one_per_value(apps, schema_editor):
    # The same address held by two workspaces becomes one row (the oldest).
    PersonAddress = apps.get_model("contacts", "PersonAddress")
    seen = set()
    for row in PersonAddress.objects.order_by("pk"):
        key = (row.person_id, row.kind, row.value)
        if key in seen:
            row.delete()
        else:
            seen.add(key)


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0027_person_addresses"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="personaddress",
            name="person_address_once_per_workspace",
        ),
        migrations.RemoveField(
            model_name="personaddress",
            name="added_by",
        ),
        migrations.RemoveField(
            model_name="personaddress",
            name="workspace",
        ),
        migrations.RunPython(keep_one_per_value, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="personaddress",
            constraint=models.UniqueConstraint(
                fields=("person", "kind", "value"), name="person_address_once"
            ),
        ),
    ]
