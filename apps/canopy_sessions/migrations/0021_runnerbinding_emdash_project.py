"""Make the emdash PROJECT half of a binding's key, instead of the name alone.

`RunnerBinding.session_key` is an emdash task NAME, and names are scoped to a
project — `issues` under `ace` and `issues` under `connect-labs` are two different
conversations. The report loop's upsert nevertheless keyed on the bare name, and
`one_binding_per_runner_session_key` enforced that reading, so the two collapsed
onto one row: the survivor kept whichever project claimed the name FIRST, and (worse)
the report deduplicated by name before upserting, so a concurrently-open namesake was
dropped from the report entirely — no row, no error.

Measured on labs 2026-08-27: session `f6f5efc0` reported `project: "ace"` while
serving a connect-labs transcript, because an `ace` task called `issues` had claimed
the name on 2026-08-14. Everything downstream already keys on the PAIR — the runner
resolves a transcript by (project, task), which is why `get_session_streams` ships
`session.emdash_project` alongside `session_key`. Only the binding didn't.

Three steps, in this order:

  1. add `emdash_project`, blank by default;
  2. backfill it from `session.emdash_project` (`project`, or the agent's slug for an
     agent chat) — so that on the first report after deploy every existing row is
     found by the new exact-match lookup and nothing forks;
  3. widen the unique constraint to (runner, emdash_project, session_key).

Step 3 can never fail on existing data: adding a column to a unique key only makes it
more permissive.

Rows already fused before this ran are NOT repaired here, because nothing in the
database says which project a fused row's history actually belongs to — the label and
the transcript disagree and only the runner knows which is right. They split on the
next report instead: the live conversation is recognised as new under its real
project and gets its own row, and the mislabelled remnant stops being reported and
retires on the normal staleness clock.
"""
from django.db import migrations, models


def backfill_emdash_project(apps, schema_editor):
    """Copy `Session.emdash_project` onto every binding.

    The property cannot be used (historical models carry no methods), so its rule is
    restated: `project`, or the agent's slug when the session is an agent chat, whose
    worktree lives under the agent's own repo. A session with neither keeps a blank,
    which the lookup's legacy branch still recognises.
    """
    RunnerBinding = apps.get_model("canopy_sessions", "RunnerBinding")

    qs = RunnerBinding.objects.select_related("session", "session__agent")
    for binding in qs.iterator():
        session = binding.session
        value = session.project or (session.agent.slug if session.agent_id else "")
        if value:
            binding.emdash_project = value[:100]
            binding.save(update_fields=["emdash_project"])


def noop(apps, schema_editor):
    """Reversible by dropping the column, which the schema step below already does.
    The data pass has nothing to undo: it only fills blanks."""


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0020_runnerbinding_transcript_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="runnerbinding",
            name="emdash_project",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.RunPython(backfill_emdash_project, noop),
        migrations.RemoveConstraint(
            model_name="runnerbinding",
            name="one_binding_per_runner_session_key",
        ),
        migrations.AddConstraint(
            model_name="runnerbinding",
            constraint=models.UniqueConstraint(
                condition=models.Q(("runner__isnull", False), models.Q(("session_key", ""), _negated=True)),
                fields=("runner", "emdash_project", "session_key"),
                name="one_binding_per_runner_project_session_key",
            ),
        ),
    ]
