"""Home every shared session and arc in a workspace (canopy-web#1289).

A row goes to its owner's sole workspace; an owner in several gets the org
default when they are in it, else the first of theirs by slug — the same place
the flat upload route used to file their other writes. An owner in none stays
NULL, and the flat /share/<token> link keeps serving it in place.
"""
from django.db import migrations, models
import django.db.models.deletion

DEFAULT_WORKSPACE_SLUG = "dimagi"


def home_rows(apps, schema_editor):
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    by_owner: dict[int, str | None] = {}

    def home_for(owner_id):
        if owner_id not in by_owner:
            slugs = sorted(
                Membership.objects.filter(user_id=owner_id).values_list("workspace_id", flat=True)
            )
            if len(slugs) == 1:
                by_owner[owner_id] = slugs[0]
            elif DEFAULT_WORKSPACE_SLUG in slugs:
                by_owner[owner_id] = DEFAULT_WORKSPACE_SLUG
            else:
                by_owner[owner_id] = slugs[0] if slugs else None
        return by_owner[owner_id]

    for name in ("Session", "SessionArc"):
        Model = apps.get_model("shared_sessions", name)
        for row in Model.objects.filter(workspace__isnull=True).only("pk", "owner_id"):
            slug = home_for(row.owner_id)
            if slug:
                Model.objects.filter(pk=row.pk).update(workspace_id=slug)


class Migration(migrations.Migration):

    dependencies = [
        ("shared_sessions", "0004_session_active_seconds"),
        ("workspaces", "0013_system_accounts"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="workspace",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="workspaces.workspace",
            ),
        ),
        migrations.AddField(
            model_name="sessionarc",
            name="workspace",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="workspaces.workspace",
            ),
        ),
        migrations.RunPython(home_rows, migrations.RunPython.noop),
    ]
