"""Every conversation from before agent CLIs ran on Claude Code, and its history is
in Claude Code's store: record that, so an agent that later lists Codex first does
not move it (`harness.clis.allowed`)."""
from django.db import migrations


def backfill(apps, schema_editor):
    Session = apps.get_model("canopy_sessions", "Session")
    Session.objects.filter(cli="").update(cli="claude-code")


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0059_cli_axis")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
