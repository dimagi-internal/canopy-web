"""A connected site is one tenant's registration — no custodian, no grants.

Folds `AppCredentialTenant` (one row per tenant granting a shared site) back
onto the site row, which now belongs to exactly one tenant. See
`AppCredential.workspace` for why.

The data step REFUSES rather than guesses on any row it cannot collapse: a site
granted by several tenants, a grant that disagrees with the row's workspace, or
a row with no tenant at all. Splitting a shared site would mean deciding which
tenant keeps its contacts and tokens, which is not a migration's call. Prod had
none of these when this was written (2026-09-24: ace-web, canopy-web and
connect-labs, each granted only by its own workspace, `connect`).
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def fold_grants_onto_sites(apps, schema_editor):
    AppCredential = apps.get_model("tokens", "AppCredential")
    AppCredentialTenant = apps.get_model("tokens", "AppCredentialTenant")

    problems = []
    for app in AppCredential.objects.all():
        grants = list(AppCredentialTenant.objects.filter(app=app))
        if len(grants) > 1:
            problems.append(f"{app.name!r} is granted by {len(grants)} tenants "
                            f"({', '.join(sorted(g.workspace_id for g in grants))})")
            continue
        grant = grants[0] if grants else None
        if app.workspace_id is None:
            if grant is None:
                problems.append(f"{app.name!r} has no workspace and no grant")
                continue
            app.workspace_id = grant.workspace_id
        elif grant is not None and grant.workspace_id != app.workspace_id:
            problems.append(f"{app.name!r} is registered by {app.workspace_id} but "
                            f"granted only by {grant.workspace_id}")
            continue
        app.resolvable_domains = list(grant.resolvable_domains or []) if grant else []
        app.save(update_fields=["workspace", "resolvable_domains"])

    if problems:
        raise RuntimeError(
            "cannot make connected sites per-tenant without a decision:\n  "
            + "\n  ".join(problems)
            + "\nResolve these rows by hand (re-register the site in each tenant) and re-run."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0020_site_outlives_its_registrant"),
        ("workspaces", "0008_rename_auto_join_domains_self_join_domains"),
    ]

    operations = [
        migrations.AddField(
            model_name="appcredential",
            name="resolvable_domains",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(fold_grants_onto_sites, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appcredential",
            name="name",
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name="appcredential",
            name="workspace",
            field=models.ForeignKey(
                help_text="The tenant this registration belongs to.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="embedded_apps",
                to="workspaces.workspace",
            ),
        ),
        migrations.AddConstraint(
            model_name="appcredential",
            constraint=models.UniqueConstraint(
                condition=Q(revoked_at__isnull=True),
                fields=("workspace", "name"),
                name="one_live_site_name_per_tenant",
            ),
        ),
        migrations.DeleteModel(name="AppCredentialTenant"),
    ]
