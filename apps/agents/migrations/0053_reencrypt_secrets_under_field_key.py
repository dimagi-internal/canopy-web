"""Move every at-rest secret off the SECRET_KEY-derived key (canopy-web#1538 Phase 0).

`reencrypt_secrets` was shipped as a post-deploy manual step, but running it needs a
one-off ECS task — a human with AWS access. A data migration gets the same result
from the deploy pipeline itself: the migration task is registered from the LIVE task
definition, which carries FIELD_ENCRYPTION_KEY since the deploy of 59d9f399.

Safe everywhere:
* FIELD_ENCRYPTION_KEY unset (dev, tests, CI, a fresh database) → no-op.
* The command is idempotent and never overwrites a row it cannot read.
* Only columns that exist at this point in the graph are touched (``existing_only``).

The legacy SECRET_KEY key stays in encryption.py's read list; dropping it is a later
change, once this has run in production.
"""
from django.conf import settings
from django.core.management import call_command
from django.db import migrations


def reencrypt(apps, schema_editor):
    if not getattr(settings, "FIELD_ENCRYPTION_KEY", ""):
        return
    call_command("reencrypt_secrets", apply=True, existing_only=True)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0052_agent_require_device_binding"),
        ("canopy_sessions", "0045_runnerbinding_close_error"),
        ("capabilities", "0005_slot_data"),
        ("contacts", "0023_memory_reach"),
        ("harness", "0075_runnerdevicekey"),
        ("slack", "0015_channel_ingest_allowlist"),
        ("tokens", "0031_hcp_trusted_contacts"),
        ("workspaces", "0013_system_accounts"),
    ]

    operations = [
        migrations.RunPython(reencrypt, migrations.RunPython.noop, elidable=True),
    ]
