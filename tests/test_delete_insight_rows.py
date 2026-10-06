"""projects/0012 deletes the retired Insights feed's rows and nothing else."""
import pytest


@pytest.mark.django_db(transaction=True)
def test_0012_deletes_only_insight_rows():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("projects", "0011_default_identity_agent_hal")]
    after = [("projects", "0012_delete_insight_rows")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    project = old.get_model("projects", "Project").objects.create(name="P", slug="p")
    Ctx = old.get_model("projects", "ProjectContext")
    for kind in ("insight", "insight", "note", "summary"):
        Ctx.objects.create(project=project, context_type=kind, content=kind, source="t")

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    left = sorted(new.get_model("projects", "ProjectContext").objects
                  .values_list("context_type", flat=True))
    assert left == ["note", "summary"]

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
