"""Contact.notes → one `role` fact each (fleet brain v1.1, canopy#804).

Its own migration, AFTER the schema one, on purpose: Postgres creates 0010's
foreign-key index from deferred SQL at the END of that migration, and rows
written by a RunPython in the same migration leave "pending trigger events"
that make the CREATE INDEX fail (caught on embedded Postgres; SQLite cannot).
"""
from django.db import migrations
from django.utils import timezone

STATEMENT_MAX = 500


def mirror_notes(apps, schema_editor):
    """Each non-empty `Contact.notes` becomes ONE `role` fact (declared, asserted
    by nobody), tagged `source_contact` — the marker that makes a re-run a
    no-op and lets a later edit supersede it. Historical models, so this keeps
    working as the live code moves; the live twin is
    `people.mirror_contact_notes` (PATCH /api/contacts/{id}/ and
    `manage.py mirror_contact_notes`). 0 rows in prod on 2026-10-07."""
    Contact = apps.get_model("contacts", "Contact")
    Person = apps.get_model("contacts", "Person")
    PersonFact = apps.get_model("contacts", "PersonFact")
    for contact in Contact.objects.exclude(notes="").iterator():
        statement = " ".join((contact.notes or "").split())[:STATEMENT_MAX]
        if not statement:
            continue
        current = (PersonFact.objects.filter(source_contact_id=contact.pk, superseded_at__isnull=True,
                                             retracted_at__isnull=True)
                   .order_by("-created_at", "-pk").first())
        if current is not None and current.statement == statement:
            continue
        person_id = contact.person_id
        if person_id is None:
            person = None
            if contact.user_id:
                person = Person.objects.filter(user_id=contact.user_id).first()
                if person is None:
                    person = Person.objects.create(user_id=contact.user_id)
            else:
                person = Person.objects.create()
            Contact.objects.filter(pk=contact.pk).update(person=person)
            person_id = person.pk
        PersonFact.objects.create(
            person_id=person_id, workspace_id=contact.workspace_id, kind="role",
            basis="declared", statement=statement, source_contact_id=contact.pk,
            supersedes=current)
        if current is not None:
            PersonFact.objects.filter(pk=current.pk).update(superseded_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0010_people_brain_v11"),
    ]

    operations = [
        migrations.RunPython(mirror_notes, migrations.RunPython.noop),
    ]
