"""Home every origin record that still has no workspace, before NULL stops
meaning "everyone".

`0002` derived each row's tenant from its authoring agent and left NULL the rows
whose agent slug resolved to nothing — "visible to any authenticated caller
until re-upserted". From this release a NULL row is visible to nobody, and
because `(repo, number)` is globally unique, an invisible row would also block
its own re-sync forever (the upsert 404s on a row the caller cannot see).

So: the agent's workspace when it resolves now, else the org default — where
every Dimagi login that could read the row yesterday still can. With neither,
the row stays NULL, which is the fail-closed answer.
"""
from django.db import migrations

DEFAULT_WORKSPACE_SLUG = "dimagi"


def home(apps, schema_editor):
    OriginIssue = apps.get_model("issues", "OriginIssue")
    Agent = apps.get_model("agents", "Agent")
    Workspace = apps.get_model("workspaces", "Workspace")
    agent_ws = dict(Agent.objects.values_list("slug", "workspace_id"))
    fallback = (
        DEFAULT_WORKSPACE_SLUG
        if Workspace.objects.filter(slug=DEFAULT_WORKSPACE_SLUG).exists()
        else None
    )
    for issue in OriginIssue.objects.filter(workspace__isnull=True).iterator():
        ws_id = agent_ws.get(issue.agent) or fallback
        if ws_id:
            issue.workspace_id = ws_id
            issue.save(update_fields=["workspace"])


class Migration(migrations.Migration):
    dependencies = [
        ("issues", "0002_originissue_workspace"),
        ("workspaces", "0010_workspace_parent"),
        ("agents", "0035_agent_delegation"),
    ]
    operations = [migrations.RunPython(home, migrations.RunPython.noop)]
