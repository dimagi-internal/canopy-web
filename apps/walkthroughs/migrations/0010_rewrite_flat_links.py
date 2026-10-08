"""Rewrite flat artifact links already stored in canopy's data (canopy-web#1337).

Flat `/walkthrough/…`, `/review/…`, `/share/…`, `/w/<uuid>/…`, `/storyboard/…`,
`/narrative/…` and `/ddd-release/…` addresses are a plain 404 now, with nothing
forwarded. This points the links canopy serves back — review payloads, agent
task links, the `<video src>` inside stored HTML decks — at the scoped address
of the artifact they name. No route is added. Idempotent; unresolvable links
are left and counted. Logic and rules: `apps/walkthroughs/flat_links.py`.

The Drive half is time-boxed (5 minutes) so it cannot stall the deploy's
migrate step; `manage.py rewrite_flat_links` finishes anything it did not
reach. It never fails the migration: a link it cannot rewrite is a link that
was already dead.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from apps.walkthroughs import flat_links

    stats = flat_links.sweep(apps, budget_s=300.0)
    print(f"\n  {flat_links.summary(stats, dry_run=False)}")


class Migration(migrations.Migration):
    # Not atomic: a rewritten deck is uploaded to Drive and the old file trashed
    # as each row is pointed at it. Rolling the rows back after that would leave
    # them naming trashed files, so each row commits as it goes.
    atomic = False

    dependencies = [
        ("walkthroughs", "0009_walkthrough_cut"),
        ("reviews", "0008_reviewrequest_suggestions_json"),
        ("shared_sessions", "0005_workspace"),
        ("storyboards", "0004_backfill_feedback_workspace"),
        ("agents", "0042_sender_trust"),
        ("shareouts", "0008_shareout_project_slug"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
