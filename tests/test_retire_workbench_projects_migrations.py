"""Retiring the workbench Projects app carries its two live pieces across.

agents/0037 copies each project's `default_identity_agent` into a RepoIdentity
row (the repo-turn GitHub identity), shareouts/0008 turns `Shareout.project`
into `project_slug`, and only then does projects/0013 drop the tables.
"""
import datetime as dt

import pytest

@pytest.mark.django_db(transaction=True)
def test_identity_mapping_and_shareout_slugs_survive_the_drop():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    # Hold workspaces at its leaf so the historical Workspace model matches the
    # table (only agents/shareouts/projects move in this test).
    workspaces_leaf = [n for n in executor.loader.graph.leaf_nodes() if n[0] == "workspaces"]
    before = [("projects", "0012_delete_insight_rows"), ("agents", "0035_agent_delegation"),
              ("shareouts", "0007_shareout_created_by"), *workspaces_leaf]
    after = [("projects", "0013_drop_workbench_projects")]
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    User = old.get_model("auth", "User")
    owner = User.objects.create(username="o")
    ws = old.get_model("workspaces", "Workspace").objects.create(
        slug="w", display_name="W", created_by=owner)
    Agent = old.get_model("agents", "Agent")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)
    Project = old.get_model("projects", "Project")
    canopy_web = Project.objects.create(name="canopy-web", slug="canopy-web", workspace=ws,
                                        default_identity_agent=hal)
    Project.objects.create(name="connect-labs", slug="connect-labs", workspace=ws,
                           default_identity_agent=echo)
    undefaulted = Project.objects.create(name="ace", slug="ace", workspace=ws)
    old.get_model("projects", "ProjectContext").objects.create(
        project=undefaulted, context_type="note", content="stale", source="t")
    Shareout = old.get_model("shareouts", "Shareout")
    day = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    for project, title in ((canopy_web, "web"), (undefaulted, "ace"), (None, "roll-up")):
        Shareout.objects.create(project=project, workspace=ws, title=title, content="c",
                                source="s", period_start=day, period_end=day)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps

    mapping = dict(new.get_model("agents", "RepoIdentity").objects
                   .values_list("repo_slug", "agent__slug"))
    assert mapping == {"canopy-web": "hal", "connect-labs": "echo"}  # "ace" had none
    slugs = dict(new.get_model("shareouts", "Shareout").objects
                 .values_list("title", "project_slug"))
    assert slugs == {"web": "canopy-web", "ace": "ace", "roll-up": None}
    tables = connection.introspection.table_names()
    assert not {"projects_project", "projects_projectcontext", "projects_projectaction"} & set(tables)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
