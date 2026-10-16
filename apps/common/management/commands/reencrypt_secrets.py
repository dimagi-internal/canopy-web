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

`AgentCredential.value_enc` additionally moves onto the `kms1:` envelope when
`VAULT_KMS_KEY_ID` is set (apps/common/kms_envelope.py); a `kms1:` value is
already off Fernet and is left alone.

Idempotent: a row already readable under the first (write) key is skipped, so a
second run reports zero. A row no configured key can read is COUNTED and left
untouched — never overwritten, never fatal to the run.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import models

from apps.common import encryption, kms_envelope

SUFFIXES = ("_enc", "_encrypted")


def secret_columns() -> list[tuple[type[models.Model], str]]:
    found = []
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for f in model._meta.concrete_fields:
            if isinstance(f, models.TextField) and f.name.endswith(SUFFIXES):
                found.append((model, f.name))
    return sorted(found, key=lambda mf: (mf[0]._meta.label, mf[1]))


class Command(BaseCommand):
    help = "Re-encrypt *_enc secret columns under the current write key (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes.")

    def handle(self, *args, apply=False, **opts):
        keys = encryption._keys()
        primary = Fernet(keys[0])
        multi = encryption._fernet()
        kms_on = kms_envelope.enabled()
        if len(keys) == 1 and not kms_on:
            self.stdout.write("Only one key is configured (FIELD_ENCRYPTION_KEY unset) and KMS is "
                              "off: every row is already under the write key. Nothing to do.")

        totals = {"rotated": 0, "to_kms": 0, "current": 0, "unreadable": 0}
        for model, field in secret_columns():
            label = f"{model._meta.label}.{field}"
            counts = dict.fromkeys(totals, 0)
            is_agent_cred = model._meta.label == "agents.AgentCredential" and field == "value_enc"
            qs = model._default_manager.exclude(**{field: ""}).only("pk", field)
            if is_agent_cred:
                qs = qs.select_related("agent").only("pk", field, "name", "agent")
            for row in qs.iterator(chunk_size=500):
                stored = getattr(row, field) or ""
                new = None
                if kms_envelope.is_envelope(stored):
                    counts["current"] += 1
                    continue
                if is_agent_cred and kms_on:
                    try:
                        plain = multi.decrypt(stored.encode()).decode()
                    except InvalidToken:
                        counts["unreadable"] += 1
                        continue
                    if apply:
                        new = row.seal(row.agent, row.name, plain)
                    counts["to_kms"] += 1
                else:
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
                    if apply:
                        new = rotated
                    counts["rotated"] += 1
                if new is not None:
                    # .update(), not .save(): no auto_now bump, no signals — the
                    # secret did not change, only its wrapping.
                    model._default_manager.filter(pk=row.pk).update(**{field: new})
            for k, v in counts.items():
                totals[k] += v
            if any(counts[k] for k in ("rotated", "to_kms", "unreadable")):
                self.stdout.write(
                    f"{label}: rotate={counts['rotated']} to_kms={counts['to_kms']} "
                    f"current={counts['current']} unreadable={counts['unreadable']}")

        verb = "rewrote" if apply else "would rewrite"
        self.stdout.write(
            f"{verb} {totals['rotated'] + totals['to_kms']} value(s) "
            f"(rotate={totals['rotated']}, to_kms={totals['to_kms']}); "
            f"{totals['current']} already current; {totals['unreadable']} unreadable"
            + ("" if apply else " — dry run, pass --apply to write"))
        if totals["unreadable"]:
            self.stderr.write("Some values no configured key can read; they were left untouched.")
