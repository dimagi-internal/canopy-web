"""HCP v1, constraint half — after 0013 backfilled every existing row."""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0013_hcp_v1_backfill"),
    ]

    operations = [
        migrations.AlterField(
            model_name="personfact",
            name="entry_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4),
        ),
        migrations.AddConstraint(
            model_name="personfact",
            constraint=models.CheckConstraint(
                condition=models.Q(("basis__in", ["declared", "inferred", "attested"])),
                name="person_fact_basis_known_v2",
            ),
        ),
        migrations.AddConstraint(
            model_name="personfact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        [
                            "general_preferences",
                            "goals_and_constraints",
                            "work_context",
                            "coordination_context",
                        ],
                    ),
                    ("category__startswith", "hcp-custom:"),
                    _connector="OR",
                ),
                name="person_fact_category_is_work_context",
            ),
        ),
        migrations.AddConstraint(
            model_name="personfact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["active", "deprecated", "conflicted", "deleted"])
                ),
                name="person_fact_status_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="personfact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("basis", "inferred"),
                        ("confidence__in", ["high", "medium", "low"]),
                    ),
                    models.Q(
                        models.Q(("basis", "inferred"), _negated=True),
                        ("confidence", ""),
                    ),
                    _connector="OR",
                ),
                name="person_fact_confidence_iff_inferred",
            ),
        ),
        migrations.AddConstraint(
            model_name="personfact",
            constraint=models.UniqueConstraint(
                fields=("entry_id", "version"),
                name="person_fact_one_row_per_entry_version",
            ),
        ),
    ]
