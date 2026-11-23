"""Triage (`brain.triage`): the cached verdicts (`BrainTriage`), and the verdict a read was served under
(`BrainRunRead.triage_score` / `triage_reason`). Additive only."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("brain", "0025_findings_quality"),
    ]

    operations = [
        migrations.AddField(
            model_name="brainrunread",
            name="triage_reason",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="brainrunread",
            name="triage_score",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="BrainTriage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("view", models.CharField(max_length=16)),
                ("after", models.IntegerField()),
                ("upto", models.IntegerField()),
                ("version", models.PositiveSmallIntegerField()),
                ("score", models.IntegerField()),
                ("signals", models.JSONField(default=dict)),
                ("reason", models.CharField(blank=True, default="", max_length=200)),
                ("made_at", models.DateTimeField(auto_now_add=True)),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="brain.brainsource",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("source", "view", "after", "upto", "version"),
                        name="brain_triage_once",
                    )
                ],
            },
        ),
    ]
