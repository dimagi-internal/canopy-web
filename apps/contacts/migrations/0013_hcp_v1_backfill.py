"""HCP v1, data half: every existing fact becomes a version of an HCP entry.

Its own migration, between the schema (0012) and the constraints (0014), for
the reason 0011 gives: on Postgres, rows written by a RunPython in the same
migration as deferred index SQL leave pending trigger events.

* A `supersedes` chain is ONE entry: the chain shares an `entry_id`, and
  `version` counts 1, 2, 3… from its root.
* `category` follows `kind` (role/project/instance → work_context; preference,
  correction, terminology → general_preferences).
* A retracted fact's entry is `deleted`.
* An inferred fact gets `confidence: medium` — HCP requires one, and nobody
  recorded one before.
* A mirrored contact note is `attested`: someone other than the person wrote it.

Historical models, so this keeps working as the live code moves.
"""
import uuid

from django.db import migrations

KIND_CATEGORY = {"role": "work_context", "project": "work_context", "instance": "work_context",
                 "preference": "general_preferences", "correction": "general_preferences",
                 "terminology": "general_preferences"}


def backfill(apps, schema_editor):
    PersonFact = apps.get_model("contacts", "PersonFact")
    rows = {f.pk: f for f in PersonFact.objects.all().order_by("pk")}
    for f in rows.values():
        chain = []
        cur = f
        while cur is not None and cur.pk not in [c.pk for c in chain]:
            chain.append(cur)
            cur = rows.get(cur.supersedes_id) if cur.supersedes_id else None
        f._depth = len(chain)                   # 1 = a root
        f._root = chain[-1].pk
    entry_of: dict[int, uuid.UUID] = {}
    for f in sorted(rows.values(), key=lambda r: (r._depth, r.pk)):
        entry = entry_of.setdefault(f._root, uuid.uuid4())
        f.entry_id = entry
        f.version = f._depth
        f.category = KIND_CATEGORY.get(f.kind, "work_context")
        f.status = "deleted" if f.retracted_at is not None else "active"
        if f.source_contact_id is not None:
            f.basis = "attested"
            f.provenance_source = f.provenance_source or f"integration:contact-notes:{f.source_contact_id}"
        if f.basis == "inferred" and not f.confidence:
            f.confidence = "medium"
        if f.basis != "inferred":
            f.confidence = ""
        if f.source_turn_id and not f.provenance_source:
            f.provenance_source = f"turn:{f.source_turn_id}"
        f.save(update_fields=["entry_id", "version", "category", "status", "basis",
                              "confidence", "provenance_source"])


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0012_hcp_v1"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
