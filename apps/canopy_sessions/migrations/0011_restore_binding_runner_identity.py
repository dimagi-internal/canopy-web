"""Give back the runner identity the old report path stripped.

Until this release, `replace_reported_sessions` nulled `RunnerBinding.runner` for
every binding that fell off a report — using one FK as both "which box owns this"
and "is it live right now". Because emdash DELETES a closed task (it never sets
`archived_at`), falling off the report is the normal end of a session's life, so the
FK was being cleared constantly: labs had 47 sessions that could not say where they
came from.

`host` is the durable identity that survived, so it is what we recover from. The
match is deliberately conservative — a binding is only re-pointed when its host maps
to EXACTLY ONE runner. Ambiguity (two runners re-paired on the same host) is left
NULL rather than guessed, since a wrong runner is worse than a missing one: claim
stickiness reads this FK, and pointing a session at the wrong box would send its
turns somewhere they can never run.

Retired runners are eligible on purpose. A retired runner is exactly the case where
the identity matters most (it is the one you cannot re-derive from a live report),
and it is now recoverable — see POST /api/harness/runners/{id}/unretire.
"""
from django.db import migrations


def restore_runner_identity(apps, schema_editor):
    RunnerBinding = apps.get_model("canopy_sessions", "RunnerBinding")
    Runner = apps.get_model("harness", "Runner")

    orphans = RunnerBinding.objects.filter(runner__isnull=True).exclude(host="")
    hosts = set(orphans.values_list("host", flat=True))
    if not hosts:
        return

    # host -> the single runner on it, or None when ambiguous.
    owner: dict[str, object] = {}
    for host in hosts:
        matches = list(Runner.objects.filter(host=host)[:2])
        owner[host] = matches[0].pk if len(matches) == 1 else None

    for binding in orphans.iterator():
        runner_pk = owner.get(binding.host)
        if runner_pk is None:
            continue
        # `one_binding_per_runner_session_key` is a partial unique index over
        # (runner, session_key) WHERE runner IS NOT NULL — which these rows have been
        # exempt from for as long as they were orphaned. Two orphans on one host can
        # therefore share a session_key (a task name reused after the first was
        # deleted). Fill only where it stays unique; a collision means the newer row
        # is the real one and the older is spent, so leaving it NULL is correct.
        if binding.session_key and RunnerBinding.objects.filter(
            runner_id=runner_pk, session_key=binding.session_key
        ).exists():
            continue
        binding.runner_id = runner_pk
        binding.save(update_fields=["runner"])


def noop(apps, schema_editor):
    """Irreversible in practice, and harmless to leave in place: the forward pass
    only fills NULLs, so re-nulling them would destroy identity to no benefit."""


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0010_reset_runner_session_messages"),
        ("harness", "0002_runner_host_sessionlink"),  # Runner.host, which we match on
    ]

    operations = [migrations.RunPython(restore_runner_identity, noop)]
