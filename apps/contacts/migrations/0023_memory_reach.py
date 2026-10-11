"""Memory reach (HCP 4.1.5.1): collections, site provenance, and agent grants that
name what they reach.

* `MemoryCollection` — a named reach the person builds from contexts
  (organisations, personal) and points agents at; `PersonGrant.collection`.
* `PersonFact.provenance_site` — the connected site an agent was reached through
  when it recorded the entry (exported as `record.provenance.site`).
* Backfill: a first-party agent's grant has always been enforced to its own
  workspace's entries, but its object carried `restrictions: []` — which 4.1.5.1
  reads as the whole category, overstating it. Each live one now names exactly
  that reach, `workspace:<slug>`. Grants issued from now on reach the agent's whole
  organisation, but an existing one is NOT silently widened — widening is only
  ever the person's act — so its reach is unchanged and no `grant.issued` is
  written (docs/architecture/hcp-conformance.md, 4.1.5.1).
"""
import django.db.models.deletion
from django.db import migrations, models


def backfill(apps, schema_editor):
    PersonGrant = apps.get_model("contacts", "PersonGrant")
    live = PersonGrant.objects.filter(status="active", hcp_client__isnull=True,
                                      agent__isnull=False, workspace__isnull=False)
    n = 0
    for g in live:
        if g.restrictions:
            continue
        g.restrictions = [{"scope": s, "narrowedTo": {"sources": [f"workspace:{g.workspace_id}"]}}
                          for s in (g.scopes or [])]
        g.save(update_fields=["restrictions"])
        n += 1
    print(f"\n  contacts.0023: named the reach of {n} live agent grant(s) (unchanged)")


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0022_retire_presumed_grants"),
    ]

    operations = [
        migrations.AddField(
            model_name="personfact",
            name="provenance_site",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name="MemoryCollection",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=80)),
                ("sources", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memory_collections",
                        to="contacts.person",
                    ),
                ),
            ],
            options={
                "db_table": "contact_memory_collections",
                "ordering": ["name", "pk"],
            },
        ),
        migrations.AddField(
            model_name="persongrant",
            name="collection",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="grants",
                to="contacts.memorycollection",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
