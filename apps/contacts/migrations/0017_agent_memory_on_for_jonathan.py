"""Agent memory starts OFF for everyone (0016) and ON for one person: Jonathan
(2026-10-09: "each person should be able to turn it on or off ... and then to
start, its only for me").

The person is resolved the way `people.by_email` does — the correspondent row
for the address, else the Person of the ONE account holding it as a verified
login address — using this migration's historical models. Both rows are turned
on when both exist: `_person_for_user` joins them, and they are the same human.
Nobody found → a no-op; nothing is created.
"""
from django.db import migrations
from django.utils import timezone

ADDRESS = "jjackson@dimagi.com"
DETAIL = "turned on at the person's request (Jonathan, 2026-10-09); off is the default for everyone"


def forwards(apps, schema_editor):
    Person = apps.get_model("contacts", "Person")
    PersonAuditEvent = apps.get_model("contacts", "PersonAuditEvent")
    EmailAddress = apps.get_model("account", "EmailAddress")

    people = list(Person.objects.filter(issuer="", signer="", email=ADDRESS))
    holders = list(EmailAddress.objects.filter(email__iexact=ADDRESS, verified=True)
                   .values_list("user_id", flat=True).distinct())
    if len(holders) == 1:
        people += list(Person.objects.filter(user_id=holders[0]))
    now = timezone.now()
    seen = set()
    for person in people:
        if person.pk in seen or person.hcp_enabled:
            continue
        seen.add(person.pk)
        person.hcp_enabled = True
        person.hcp_enabled_changed_at = now
        person.save(update_fields=["hcp_enabled", "hcp_enabled_changed_at"])
        PersonAuditEvent.objects.create(person=person, event_type="agentAccess.enabled",
                                        actor_id="canopy", actor_type="system", detail=DETAIL)


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0016_person_agent_memory"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
