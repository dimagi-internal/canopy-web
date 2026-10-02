"""Fold report-only close-out rows into the turns they were closing.

Before #1057/#1063, a cloud-runner agent turn had no session key when its agent
posted the close-out, so `upsert_turn` could not find it and created a second,
report-only Turn (`idempotency_key` "closeout:<agent>:<claude session>") beside
it — echo's Turns page shows a pair for every cloud turn. New close-outs join;
this repairs the old ones.

Conservative by construction — a row is merged only when its turn is certain:

1. EXACT: an unreported turn of the same agent whose session_key is the
   report's Claude session id (the 2026-10-02 17:00 case: keyed at finish,
   reported a few seconds before).
2. CLOUD WINDOW: the report was posted while exactly ONE unreported, unkeyed
   turn of that agent was running on a cloud runner (started before the report,
   finished no more than SLACK after it). A cloud agent turn's close-out is
   posted from inside that turn, so one candidate is the turn.

Everything else is left as it is: laptop turns (their report arrives long
after the 5-second dispatch, so no window can say which turn), a turn a human
started by hand, two overlapping candidates. A report row that has its own
events is never deleted. Irreversible in the sense that a merged pair is not
split again; the reverse is a no-op.
"""

import datetime as dt

from django.db import migrations

SLACK = dt.timedelta(minutes=10)

REPORT_FIELDS = (
    "report_title", "report_summary", "task_ext_ids", "work_product_urls",
    "session_slug", "share_token", "report_source", "cli_session_id", "reported_at",
)


def merge_orphan_closeouts(Turn, TurnEvent) -> int:
    """Merge every orphaned close-out it can place. Returns how many it merged.
    Takes the models so a test can run it against the live ones."""
    merged = 0
    reports = (
        Turn.objects.filter(
            idempotency_key__startswith="closeout:",
            agent__isnull=False,
            reported_at__isnull=False,
        )
        .order_by("created_at")
    )
    for report in reports:
        if TurnEvent.objects.filter(turn_id=report.pk).exists():
            continue
        candidates = Turn.objects.filter(
            agent_id=report.agent_id, reported_at__isnull=True
        ).exclude(pk=report.pk)

        turn = None
        if report.cli_session_id:
            turn = (
                candidates.filter(session_key=report.cli_session_id)
                .order_by("-created_at")
                .first()
            )
        if turn is None:
            window = list(
                candidates.filter(
                    claimed_by__kind="cloud",
                    session_key="",
                    started_at__isnull=False,
                    started_at__lte=report.created_at,
                    finished_at__isnull=False,
                    finished_at__gte=report.created_at - SLACK,
                )[:2]
            )
            if len(window) == 1:
                turn = window[0]
        if turn is None:
            continue

        values = {f: getattr(report, f) for f in REPORT_FIELDS}
        # The report holds (agent, cli_session_id), which is unique — it goes
        # first, then the turn takes it.
        report.delete()
        for field, value in values.items():
            setattr(turn, field, value)
        if not turn.session_key and turn.cli_session_id:
            turn.session_key = turn.cli_session_id
        turn.save()
        merged += 1
    return merged


def forwards(apps, schema_editor):
    merge_orphan_closeouts(apps.get_model("harness", "Turn"), apps.get_model("harness", "TurnEvent"))


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0057_turn_session_key"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
