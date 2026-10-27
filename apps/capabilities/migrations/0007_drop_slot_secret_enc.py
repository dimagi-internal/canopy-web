"""Drop `CapabilitySlotData.secret_enc`, the pre-sealing column canopy-web could decrypt.
Every slot secret is sealed (`secret_zk`) now.

Guarded: a row still holding a value there stops the migration rather than drop
the one copy of a secret. Reversing re-adds an empty column.
"""
from django.db import migrations


def refuse_to_lose_a_value(apps, schema_editor):
    CapabilitySlotData = apps.get_model("capabilities", "CapabilitySlotData")
    stranded = sorted(f"{cap}/{slot}" for cap, slot in
                      CapabilitySlotData.objects.exclude(secret_enc="")
                      .values_list("capability__name", "slot"))
    if stranded:
        raise RuntimeError("Refusing to drop CapabilitySlotData.secret_enc: these slots still hold "
                           "unsealed secret data — " + ", ".join(stranded) + ". Write their secrets "
                           "again (PUT …/slots/{slot} seals them), then migrate again.")


class Migration(migrations.Migration):
    dependencies = [
        ("capabilities", "0006_slot_secret_zk"),
    ]

    operations = [
        migrations.RunPython(refuse_to_lose_a_value, migrations.RunPython.noop),
        migrations.RemoveField(model_name="capabilityslotdata", name="secret_enc"),
    ]
