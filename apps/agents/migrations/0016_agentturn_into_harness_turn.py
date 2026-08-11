"""Carry the close-out reports off AgentTurn and onto harness.Turn, so one row
holds both halves of a turn.

Two independent steps, both idempotent and both safe to run against a live table:

1. Backfill `Turn.emdash_task_id` from `result_note`. The runner has always written
   the session it created as prose ("created session 'hal-api-df02-0810-0805'") and
   never as a key. Parsing it once here is what lets turns dispatched BEFORE this
   deploy still be claimed by a close-out afterwards — including sessions that are
   live at deploy time, which are exactly the ones about to close out.

2. Copy every AgentTurn into a report-only Turn.

Step 2 does NOT attempt to join history. Measured on the live fleet the day this was
written: 0 of 30 AgentTurn rows shared a key with any harness Turn (cli_session_id
was never reported to the harness, and `session_id` was populated on 44/46 *lost*
turns but only 13/211 done ones). There is no key to join on, so inventing a
heuristic — nearest timestamp, say — would fabricate provenance for the permanent
record. They come across as standalone rows: honest, and visibly report-only
because they carry `reported_at` with no dispatch fields.
"""
from django.db import migrations

# "created session 'name'" / "created session 'name' (rehydrated)" — the runner's
# own wording in execute.py. Anchored to the quotes so a note that merely mentions
# a session cannot match.
_CREATED_SESSION = r"created session '([^']+)'"


def forwards(apps, schema_editor):
    Turn = apps.get_model("harness", "Turn")
    AgentTurn = apps.get_model("agents", "AgentTurn")

    # --- 1. backfill the join key from the runner's prose ---
    import re

    pattern = re.compile(_CREATED_SESSION)
    to_fix = []
    for turn in Turn.objects.filter(emdash_task_id="").exclude(result_note="").iterator():
        match = pattern.search(turn.result_note or "")
        if match:
            turn.emdash_task_id = match.group(1)[:200]
            to_fix.append(turn)
    if to_fix:
        Turn.objects.bulk_update(to_fix, ["emdash_task_id"], batch_size=500)

    # --- 2. bring the reports across ---
    for old in AgentTurn.objects.select_related("agent").iterator():
        key = f"closeout:{old.agent.slug}:{old.cli_session_id}"
        if Turn.objects.filter(idempotency_key=key).exists():
            continue  # re-run of this migration, or the row already came across
        new = Turn.objects.create(
            agent=old.agent,
            project="",
            origin="api",
            status="done",
            routing="prefer_local",
            idempotency_key=key,
            cli_session_id=old.cli_session_id,
            report_title=old.title,
            report_summary=old.summary,
            task_ext_ids=list(old.task_ext_ids or []),
            work_product_urls=list(old.work_product_urls or []),
            session_slug=old.session_slug,
            share_token=old.share_token,
            report_source=old.source,
            reported_at=old.created_at,
            started_at=old.started_at,
            finished_at=old.ended_at,
        )
        # created_at is auto_now_add, so it is stamped NOW on create and has to be
        # put back — otherwise every migrated turn reads as having happened at
        # deploy time and the workspace timeline is nonsense.
        Turn.objects.filter(pk=new.pk).update(created_at=old.created_at)


def backwards(apps, schema_editor):
    """Drop only the rows this migration created. The emdash_task_id backfill is
    left in place: it is derived from data that was already there, nothing reads it
    that did not exist before, and re-deriving it is free."""
    Turn = apps.get_model("harness", "Turn")
    Turn.objects.filter(idempotency_key__startswith="closeout:").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0015_alter_agent_turn_mode"),
        # The report fields have to exist before anything can be written into them.
        ("harness", "0037_turn_cli_session_id_turn_emdash_task_id_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
