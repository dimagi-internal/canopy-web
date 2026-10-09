"""Defaults for the per-session model (0018).

Everyone who had a feature on keeps it available and on by default — the
behaviour they had. Jonathan's own canopy level is then set as he asked
(2026-10-09: "mine should be record yes, use no"): record available and on by
default; use NOT available, so no session can turn it on until he makes it
available. Each change is its own audit event. Nobody found → only the
general carry-over runs; nothing is created.
"""
from django.db import migrations
from django.utils import timezone

ADDRESS = "jjackson@dimagi.com"
DETAIL = "set at the person's request (Jonathan, 2026-10-09): record yes, use no"


def _jonathan(apps):
    Person = apps.get_model("contacts", "Person")
    EmailAddress = apps.get_model("account", "EmailAddress")
    people = list(Person.objects.filter(issuer="", signer="", email=ADDRESS))
    holders = list(EmailAddress.objects.filter(email__iexact=ADDRESS, verified=True)
                   .values_list("user_id", flat=True).distinct())
    if len(holders) == 1:
        people += list(Person.objects.filter(user_id=holders[0]))
    return list({p.pk: p for p in people}.values())


def forwards(apps, schema_editor):
    Person = apps.get_model("contacts", "Person")
    PersonAuditEvent = apps.get_model("contacts", "PersonAuditEvent")
    jonathan = _jonathan(apps)
    others = Person.objects.exclude(pk__in=[p.pk for p in jonathan])
    others.filter(hcp_record_available=True).update(hcp_record_default=True)
    others.filter(hcp_use_available=True).update(hcp_use_default=True)
    now = timezone.now()
    want = {"hcp_record_available": True, "hcp_record_default": True,
            "hcp_use_available": False, "hcp_use_default": False}
    events = {"hcp_record_available": ("agentRecord.enabled", "agentRecord.disabled"),
              "hcp_record_default": ("agentRecord.defaultOn", "agentRecord.defaultOff"),
              "hcp_use_available": ("agentUse.enabled", "agentUse.disabled"),
              "hcp_use_default": ("agentUse.defaultOn", "agentUse.defaultOff")}
    for person in jonathan:
        fields, written = [], []
        for name, value in want.items():
            if getattr(person, name) == value:
                continue
            setattr(person, name, value)
            fields.append(name)
            written.append(events[name][0 if value else 1])
            stamp = name.rsplit("_", 1)[0] + "_changed_at"
            setattr(person, stamp, now)
            if stamp not in fields:
                fields.append(stamp)
        if not fields:
            continue
        person.save(update_fields=fields)
        for event in written:
            PersonAuditEvent.objects.create(person=person, event_type=event, actor_id="canopy",
                                            actor_type="system", detail=DETAIL)


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0018_agent_memory_per_session"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
