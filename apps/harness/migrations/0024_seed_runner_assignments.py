from django.db import migrations


def seed(apps, schema_editor):
    # Pass the HISTORICAL models. The helper's rule is model-shape-independent
    # (it reads JSON fields and sorts), but the QUERY is not: called against the
    # live models it selects every column `Runner` has today, and at this point
    # in the migration graph the table only has the columns of its day. That made
    # `migrate` from zero fail the moment a new Runner field was added — see the
    # docstring on seed_assignments_from_capabilities.
    from apps.harness.services import seed_assignments_from_capabilities

    seed_assignments_from_capabilities(
        agent_model=apps.get_model("agents", "Agent"),
        runner_model=apps.get_model("harness", "Runner"),
        assignment_model=apps.get_model("harness", "RunnerAssignment"),
    )


class Migration(migrations.Migration):
    dependencies = [("harness", "0023_turn_pinned_runner_alter_item_origin_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
