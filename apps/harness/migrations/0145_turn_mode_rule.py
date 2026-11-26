"""Turn mode gets its own rules, apart from routing (`TurnModeRule`).

Creates the table and copies every routing rule's mode into it: one turn-mode
rule per (agent, source, actor) whose routing rows carried a mode. The routing
rows themselves are left as they are; 0146 drops their `turn_mode` column.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_modes(apps, schema_editor):
    RunnerAssignment = apps.get_model("harness", "RunnerAssignment")
    TurnModeRule = apps.get_model("harness", "TurnModeRule")
    seen = {}
    rows = (RunnerAssignment.objects.exclude(source="").exclude(turn_mode="")
            .order_by("agent_id", "source", "actor", "rank")
            .values_list("agent_id", "source", "actor", "turn_mode"))
    for agent_id, source, actor, mode in rows:
        # Rule-level: every row of a rule carries the same mode; take the first.
        seen.setdefault((agent_id, source, actor), mode)
    TurnModeRule.objects.bulk_create([
        TurnModeRule(agent_id=agent_id, source=source, actor=actor, turn_mode=mode)
        for (agent_id, source, actor), mode in seen.items() if mode in ("manual", "auto")
    ])


def copy_back(apps, schema_editor):
    RunnerAssignment = apps.get_model("harness", "RunnerAssignment")
    TurnModeRule = apps.get_model("harness", "TurnModeRule")
    for rule in TurnModeRule.objects.exclude(source=""):
        RunnerAssignment.objects.filter(
            agent_id=rule.agent_id, source=rule.source, actor=rule.actor,
        ).update(turn_mode=rule.turn_mode)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0147_project_files"),
        ("harness", "0144_act_as_grant_push_sent_gone"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TurnModeRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(blank=True, default="", max_length=32)),
                ("actor", models.CharField(blank=True, default="", max_length=254)),
                ("turn_mode", models.CharField(choices=[("manual", "Manual: outbound actions wait for approval"), ("auto", "Auto: the agent acts and reports")], max_length=8)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="turn_mode_rules", to="agents.agent")),
                ("set_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["agent_id", "actor", "source"],
                "constraints": [
                    models.UniqueConstraint(fields=("agent", "source", "actor"), name="one_turn_mode_rule_per_agent_source_actor"),
                    models.CheckConstraint(condition=models.Q(("actor", ""), ("source", ""), _negated=True), name="turn_mode_rule_names_a_source_or_a_person"),
                    models.CheckConstraint(condition=models.Q(("turn_mode__in", ["manual", "auto"])), name="turn_mode_rule_mode_is_manual_or_auto"),
                ],
            },
        ),
        migrations.RunPython(copy_modes, copy_back),
    ]
