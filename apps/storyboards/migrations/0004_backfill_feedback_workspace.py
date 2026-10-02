"""Attribute pre-existing Feedback rows to a tenant.

Lives in the PRODUCT app because the evidence does: a feedback row names its
target by slug (a storyboard, or a narrative on one), and only storyboards and
walkthroughs know which workspace a slug belongs to. The framework app
(`feedback`) must never import them.

A row is assigned only when the evidence names exactly ONE workspace; otherwise
the submitter's sole membership; otherwise it stays NULL — which is visible to
nobody. Fail closed: a guess would put one tenant's reviewer notes in another's
pool, which is the leak this column exists to end.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Feedback = apps.get_model("feedback", "Feedback")
    Storyboard = apps.get_model("storyboards", "Storyboard")
    Entry = apps.get_model("storyboards", "Entry")
    Walkthrough = apps.get_model("walkthroughs", "Walkthrough")
    Membership = apps.get_model("workspaces", "WorkspaceMembership")

    for fb in Feedback.objects.filter(workspace__isnull=True).iterator():
        if fb.target_kind == "storyboard":
            candidates = set(
                Storyboard.objects.filter(slug=fb.target_ref).values_list("workspace_id", flat=True)
            )
        else:
            candidates = set(
                Entry.objects.filter(narrative_slug=fb.target_ref)
                .values_list("act__storyboard__workspace_id", flat=True)
            ) | set(
                Walkthrough.objects.filter(narrative_slug=fb.target_ref)
                .exclude(workspace__isnull=True)
                .values_list("workspace_id", flat=True)
            )
        if len(candidates) != 1 and fb.submitted_by_id:
            candidates = set(
                Membership.objects.filter(user_id=fb.submitted_by_id)
                .values_list("workspace_id", flat=True)
            )
        if len(candidates) == 1:
            fb.workspace_id = candidates.pop()
            fb.save(update_fields=["workspace"])


class Migration(migrations.Migration):
    dependencies = [
        ("storyboards", "0003_entry_blurb_entry_title_storyboard_layout"),
        ("feedback", "0002_feedback_workspace"),
        ("walkthroughs", "0008_mint_share_tokens"),
        ("workspaces", "0010_workspace_parent"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
