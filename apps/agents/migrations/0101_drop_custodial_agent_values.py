"""An agent's vault is zero-knowledge only: drop `AgentCredential.value_enc`, the
custodial column canopy-web could decrypt.

Guarded: a row whose only value is custodial (`value_enc` set, `value_zk` empty)
stops the migration with the names, rather than drop the one copy of a secret.
Convert those agents first (or delete the values), then deploy again. A row that
also holds `value_zk` loses only its stale custodial copy.

Reversing re-adds an empty column: the copies it held do not come back.
"""
from django.db import migrations


def refuse_to_lose_a_value(apps, schema_editor):
    AgentCredential = apps.get_model("agents", "AgentCredential")
    stranded = sorted(
        f"{slug}:{name}" for slug, name in
        AgentCredential.objects.exclude(value_enc="").filter(value_zk="")
        .values_list("agent__slug", "name")
    )
    if stranded:
        raise RuntimeError(
            "Refusing to drop AgentCredential.value_enc: these values are stored only "
            "custodially and would be lost — " + ", ".join(stranded) + ". Re-store each "
            "one (PUT /api/agents/<slug>/credentials seals it) or delete it, then migrate again.")


class Migration(migrations.Migration):
    dependencies = [("agents", "0100_vault_entry_account")]

    operations = [
        migrations.RunPython(refuse_to_lose_a_value, migrations.RunPython.noop),
        migrations.RemoveField(model_name="agentcredential", name="value_enc"),
    ]
