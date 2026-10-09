"""Retire every presumed first-party grant (HCP 4.1.6).

Until now the canopy control plane issued an agent a persistent grant the first
time it served a person (`modality=canopy-control-plane`). Jonathan, 2026-10-09:
grants are per agent and only the person's act creates one ("each agent session /
entry should obtain the grant explicitly or due to previous granting to this
agent"). No presumed grant was such an act, so none qualifies to be kept: each
active one is revoked, with a `grant.revoked` event on the person's own log
saying why. Policy (`Person.hcp_*_available/default`) is untouched, so a person
whose default is on is offered the grant in their next session.
"""
import uuid

from django.db import migrations
from django.utils import timezone

PRESUMED = "canopy-control-plane"
DETAIL = ("{client}: a grant canopy had presumed is retired — grants are now only ever "
          "given by you, per agent, in a session (HCP 4.1.6). Nothing was deleted.")


def forwards(apps, schema_editor):
    PersonGrant = apps.get_model("contacts", "PersonGrant")
    PersonAuditEvent = apps.get_model("contacts", "PersonAuditEvent")
    now = timezone.now()
    live = list(PersonGrant.objects.filter(modality=PRESUMED, status="active",
                                           hcp_client__isnull=True))
    for g in live:
        PersonGrant.objects.filter(pk=g.pk).update(status="revoked", revoked_at=now)
        PersonAuditEvent.objects.create(
            event_id=uuid.uuid4(), person_id=g.person_id, event_type="grant.revoked",
            actor_id="canopy", actor_type="system", grant_id=g.grant_id,
            workspace_id=g.workspace_id, detail=DETAIL.format(client=g.client_name))


class Migration(migrations.Migration):
    dependencies = [("contacts", "0021_hcp_service")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
