"""Per-source routing rules on RunnerAssignment (spec 2026-07-27).

Splitting the old unique constraint cannot fail on existing data: every current
row has source="" and so lands in the first constraint, which is the old one
plus a condition that all of them satisfy.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0030_source_vocabulary")]

    operations = [
        migrations.AddField(
            model_name="runnerassignment",
            name="source",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="runnerassignment",
            name="strict",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(
            model_name="runnerassignment", name="one_assignment_per_agent_runner",
        ),
        migrations.AddConstraint(
            model_name="runnerassignment",
            constraint=models.UniqueConstraint(
                fields=("agent", "runner"),
                condition=models.Q(source=""),
                name="one_default_assignment_per_agent_runner",
            ),
        ),
        migrations.AddConstraint(
            model_name="runnerassignment",
            constraint=models.UniqueConstraint(
                fields=("agent", "source"),
                condition=models.Q(("source", ""), _negated=True),
                name="one_priority_runner_per_agent_source",
            ),
        ),
    ]
