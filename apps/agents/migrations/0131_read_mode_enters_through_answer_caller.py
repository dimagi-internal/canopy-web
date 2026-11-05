"""A published read mode that starts its session with the agent's `/<slug>:ask` command now starts it
with `/<slug>:answer-caller`, the skill that command only pointed at.

The command name was the last place a person met read mode's old name. Canopy starts a read-mode
session BY the entry stored on the agent, so the agent repos can retire their `commands/ask.md` only
once no stored entry names it. Only an agent whose published skill catalog has `answer-caller` moves
(an agent without it keeps its entry, and still works); only the command name changes, never its
arguments. Nothing about who may use read mode changes, and read mode's stored name stays
(`interface_default.READ_MODE_STAMP`).

Each moved agent's old and new entry are kept in the settings audit (`SettingChange`, operation
`read_mode_migration`), as `0119` did.
"""
import re

from django.db import migrations

_SKILL = "answer-caller"


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    Skill = apps.get_model("agents", "AgentSkill")
    Change = apps.get_model("events", "SettingChange")
    with_skill = set(Skill.objects.filter(name=_SKILL).values_list("agent_id", flat=True))
    for agent in Agent.objects.filter(pk__in=with_skill).order_by("pk"):
        caps = (agent.interface or {}).get("capabilities") or {}
        old_command = re.compile(rf"^/{re.escape(agent.slug)}:ask(?=\s|$)")
        changes = []
        for name, entry in caps.items():
            old = (entry or {}).get("entry")
            if isinstance(old, str) and old_command.match(old):
                entry["entry"] = old_command.sub(f"/{agent.slug}:{_SKILL}", old)
                changes.append({"scope": f"read-mode:{agent.slug}", "changed": True,
                                "before": {"profile": name, "entry": old},
                                "after": {"profile": name, "entry": entry["entry"]}})
        if not changes:
            continue
        agent.save(update_fields=["interface"])
        Change.objects.create(
            workspace_id=agent.workspace_id, actor=None, actor_email="", via="system",
            operation="read_mode_migration", method="MIGRATE", path="agents/0131", target=agent.slug,
            summary=f"{agent.slug}: read mode now starts with /{agent.slug}:{_SKILL}"[:500],
            changes=changes)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0130_project_member"),
        ("events", "0009_session_usage_workspace_required"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
