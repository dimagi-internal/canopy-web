"""Re-encrypt every at-rest secret column under the CURRENT write key.

    manage.py reencrypt_secrets            # dry run: counts only, writes nothing
    manage.py reencrypt_secrets --apply    # rewrite the rows that need it

canopy-web#1538 Phase 0. `apps/common/encryption.py` writes under
`FIELD_ENCRYPTION_KEY` and still reads under the legacy `SECRET_KEY`-derived key;
this moves every row written under the legacy key onto the new one
(`MultiFernet.rotate`), so the legacy key can later be dropped. Run it AFTER the
deploy that provisions `FIELD_ENCRYPTION_KEY` — with only the legacy key
configured there is nothing to rotate to, and it says so.

Columns are DISCOVERED, not listed: every model TextField named `*_enc` or
`*_encrypted`. A hand-kept list is the thing that silently misses the next one.

Idempotent: a row already readable under the first (write) key is skipped, so a
second run reports zero. A row no configured key can read is COUNTED and left
untouched — never overwritten, never fatal to the run.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import models

from apps.common import encryption

SUFFIXES = ("_enc", "_encrypted")


def secret_columns(existing_only: bool = False) -> list[tuple[type[models.Model], str]]:
    """Every (model, field) secret column. ``existing_only`` keeps just the columns
    the database has right now — the data migration (agents 0053) runs at a fixed
    point in the migration graph, where a model added or changed by a LATER
    migration may not have its column yet; querying it would fail the migration."""
    found = []
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for f in model._meta.concrete_fields:
            if isinstance(f, models.TextField) and f.name.endswith(SUFFIXES):
                found.append((model, f.name))
    if existing_only:
        from django.db import connection
        # Describe only the tables that hold a secret column — describing every table
        # trips SQLite's introspection on tables unrelated to this.
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
            columns = {
                t: {c.name for c in connection.introspection.get_table_description(cursor, t)}
                for t in {m._meta.db_table for m, _ in found} & tables
            }
        found = [(m, f) for m, f in found
                 if m._meta.get_field(f).column in columns.get(m._meta.db_table, set())]
    return sorted(found, key=lambda mf: (mf[0]._meta.label, mf[1]))


class Command(BaseCommand):
    help = "Re-encrypt *_enc secret columns under the current write key (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument("--existing-only", action="store_true",
                            help="Skip columns the database does not have yet (used by the data migration).")

    def handle(self, *args, apply=False, existing_only=False, **opts):
        keys = encryption._keys()
        primary = Fernet(keys[0])
        multi = encryption._fernet()
        if len(keys) == 1:
            self.stdout.write("Only one key is configured (FIELD_ENCRYPTION_KEY unset): every row "
                              "is already under the write key. Nothing to do.")

        totals = {"rotated": 0, "current": 0, "unreadable": 0}
        for model, field in secret_columns(existing_only=existing_only):
            label = f"{model._meta.label}.{field}"
            counts = dict.fromkeys(totals, 0)
            qs = model._default_manager.exclude(**{field: ""}).only("pk", field)
            for row in qs.iterator(chunk_size=500):
                stored = getattr(row, field) or ""
                try:
                    primary.decrypt(stored.encode())
                    counts["current"] += 1
                    continue
                except InvalidToken:
                    pass
                try:
                    rotated = multi.rotate(stored.encode()).decode()
                except InvalidToken:
                    counts["unreadable"] += 1
                    continue
                counts["rotated"] += 1
                if apply:
                    # .update(), not .save(): no auto_now bump, no signals — the
                    # secret did not change, only its wrapping.
                    model._default_manager.filter(pk=row.pk).update(**{field: rotated})
            for k, v in counts.items():
                totals[k] += v
            if counts["rotated"] or counts["unreadable"]:
                self.stdout.write(
                    f"{label}: rotate={counts['rotated']} "
                    f"current={counts['current']} unreadable={counts['unreadable']}")

        verb = "rewrote" if apply else "would rewrite"
        self.stdout.write(
            f"{verb} {totals['rotated']} value(s); "
            f"{totals['current']} already current; {totals['unreadable']} unreadable"
            + ("" if apply else " — dry run, pass --apply to write"))
        if totals["unreadable"]:
            self.stderr.write("Some values no configured key can read; they were left untouched.")
