"""A site visitor's `Person` is keyed on the SIGNER, not the site row.

Each tenant now registers a connected system itself (`tokens.0021`), so the
same system is several rows. See `Person.signer` for why the signer is the
right key and why it never merges two different systems.
"""

import hashlib

from django.db import migrations, models


def _signer(app) -> str:
    # Frozen copy of `AppCredential.signer()` — a migration must not import
    # model methods that may change after it is written.
    if app.jwks_url:
        source = "jwks\n" + app.jwks_url.strip()
    elif app.public_keys:
        source = "keys\n" + "\n".join(sorted(k.strip() for k in app.public_keys))
    else:
        return ""
    return hashlib.sha256(source.encode()).hexdigest()


def key_people_on_their_signer(apps, schema_editor):
    Person = apps.get_model("contacts", "Person")
    Contact = apps.get_model("contacts", "Contact")

    keep: dict[tuple[str, str, str], int] = {}
    for person in Person.objects.exclude(app__isnull=True).select_related("app").order_by("pk"):
        signer = _signer(person.app)
        # A row that signs with nothing still needs a distinct key, or every
        # such site's visitors would collapse into one namespace.
        if not signer:
            signer = f"row:{person.app_id}"
        key = (person.app.name, signer, person.external_id)
        if key in keep:
            # Two registrations of the same signer already knew this visitor:
            # the same human, which is what this model exists to record.
            Contact.objects.filter(person_id=person.pk).update(person_id=keep[key])
            person.delete()
            continue
        keep[key] = person.pk
        person.issuer, person.signer = key[0], key[1]
        person.save(update_fields=["issuer", "signer"])


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0006_one_person_across_tenants"),
        ("tokens", "0021_site_belongs_to_one_tenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="issuer",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="person",
            name="signer",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(key_people_on_their_signer, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="person", name="one_person_per_site_visitor"),
        migrations.RemoveConstraint(model_name="person", name="one_person_per_correspondent"),
        migrations.RemoveField(model_name="person", name="app"),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.UniqueConstraint(
                condition=models.Q(external_id__gt=""),
                fields=("issuer", "signer", "external_id"),
                name="one_person_per_signer_visitor",
            ),
        ),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.UniqueConstraint(
                condition=models.Q(signer="") & models.Q(email__gt=""),
                fields=("email",),
                name="one_person_per_correspondent",
            ),
        ),
    ]
