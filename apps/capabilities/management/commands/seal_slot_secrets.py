"""Seal every pool slot's pre-sealing secret data, once, after the deploy that adds `secret_zk`.

Before sealing, a slot's secret map was Fernet ciphertext under canopy-web's own
key (`CapabilitySlotData.secret_enc`). This opens each such row ONE last time,
seals every value to the agent's current readers (`slot_data._seal`, the same
seal a plaintext write gets), stores the result in `secret_zk` and clears
`secret_enc`, one row per transaction. A row whose agent nobody can read yet (no
vault key, no approved machine) is left as it was and named; run this again once
someone can. Prints key names and counts, never a value. For an operator in an
ECS Exec shell; `--dry-run` reports without writing.

This is a command, not a data migration, because it needs the vault's sealing
code and the agent's readers as they are now, which a migration must not import.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.capabilities import services, slot_data
from apps.capabilities.models import CapabilitySlotData
from apps.common.encryption import decrypt_secret


class Command(BaseCommand):
    help = "Seal pool slots' legacy secret data (secret_enc) to each agent's readers, then clear it."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be sealed; write nothing.")

    def handle(self, *args, dry_run=False, **options):
        sealed, skipped = [], []
        ids = list(CapabilitySlotData.objects.exclude(secret_enc="").values_list("pk", flat=True))
        for pk in ids:
            with transaction.atomic():
                row = (CapabilitySlotData.objects.select_for_update(of=("self",))
                       .select_related("capability__agent").filter(pk=pk).exclude(secret_enc="").first())
                if row is None:
                    continue
                cap = row.capability
                where = f"{cap.agent.slug}/{cap.name}/{row.slot}"
                legacy = json.loads(decrypt_secret(row.secret_enc))
                try:
                    out = {k: slot_data._seal(cap, row.slot, k, v) for k, v in legacy.items()}
                except services.CapabilityError as e:
                    skipped.append({"slot": where, "keys": sorted(legacy), "why": e.detail})
                    continue
                finally:
                    del legacy
                if not dry_run:
                    row.secret_zk = {**out, **(row.secret_zk or {})}
                    row.secret_enc = ""
                    row.save(update_fields=["secret_zk", "secret_enc"])
                    services._cap_event(cap, "slot.secret_sealed",
                                        f"{where}: legacy secret data sealed to the agent's readers",
                                        slot=row.slot, secret_keys=sorted(out))
                sealed.append({"slot": where, "keys": sorted(out)})
        for s in skipped:
            self.stderr.write(f"left unsealed: {s['slot']} ({', '.join(s['keys'])}): {s['why']}")
        self.stdout.write(json.dumps({"dry_run": dry_run, "sealed": sealed, "skipped": skipped}, sort_keys=True))
