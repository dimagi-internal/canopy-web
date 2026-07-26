"""Throw away canopy's derived view of chat and re-derive it from the runners.

Once the transcript is a session's durable record, canopy's `Message` rows stop
being data and become a CACHE of files on the runner's disk. That makes reset the
cheap operation it should be: drop the rows, ask the runner for the transcript,
and the conversation rebuilds — including everything the old per-turn projection
could never capture. The careful per-session migration this replaces was treating
a cache like an archive.

The same applies one level up: `RunnerBinding`s and runner-origin `Session`s are
re-materialized by the next session report (every 10s), so pruning a zombie costs
nothing.

    manage.py reset_chat_state --dry-run              # what would change
    manage.py reset_chat_state                        # every servable session
    manage.py reset_chat_state --session <id>         # just one
    manage.py reset_chat_state --workspace dimagi     # one tenant
    manage.py reset_chat_state --prune-ghosts         # + drop unservable zombies

WHAT IS NOT A CACHE, and is therefore never touched here: `Turn`s and their event
ledger (canopy's own record of what it ran — not derivable from anything), and
sessions no live runner can serve. A session whose emdash task is gone still has
its transcript on disk, but nothing reports it any more, so canopy's copy is the
ONLY copy — those are skipped by default and only dropped under --prune-ghosts.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner

# A runner only has to be REACHABLE to ship a transcript, not ready to run turns —
# mirrors services.request_backfill, which reads the transcript FILE and never
# needs emdash's CDP port.
SERVABLE = {Runner.ONLINE, Runner.DEGRADED}


class Command(BaseCommand):
    help = "Reset chat state: drop derived Message rows and re-derive from runner transcripts."

    def add_arguments(self, parser):
        parser.add_argument("--session", help="Reset just this session id.")
        parser.add_argument("--workspace", help="Limit to one workspace slug.")
        parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
        parser.add_argument(
            "--prune-ghosts", action="store_true",
            help="Also DELETE runner-origin sessions no live runner reports (their "
                 "transcripts are unreachable, so canopy holds the only copy).",
        )

    def handle(self, *args, **opts):
        sessions = Session.objects.all().order_by("created_at")
        if opts["session"]:
            sessions = sessions.filter(pk=opts["session"])
            if not sessions.exists():
                raise CommandError(f"no such session: {opts['session']}")
        if opts["workspace"]:
            sessions = sessions.filter(workspace_id=opts["workspace"])

        bindings = {
            b.session_id: b
            for b in RunnerBinding.objects.select_related("runner").filter(
                session__in=sessions.values("pk")
            )
        }
        dry = opts["dry_run"]
        reset = skipped = pruned = rows_dropped = 0

        for session in sessions:
            binding = bindings.get(session.id)
            servable = (
                binding is not None
                and binding.runner_id is not None
                and binding.runner.live_status in SERVABLE
            )
            if not servable:
                skipped += 1
                if opts["prune_ghosts"] and session.origin == Session.ORIGIN_RUNNER:
                    pruned += 1
                    self.stdout.write(f"  prune  {session.id} {session.title[:40]!r} (no live runner)")
                    if not dry:
                        session.delete()
                else:
                    self.stdout.write(
                        f"  skip   {session.id} {session.title[:40]!r} — "
                        f"{'no runner binding' if binding is None else 'runner not reachable'}"
                        f"{'; canopy holds the only copy' if session.messages.exists() else ''}"
                    )
                continue

            n = Message.objects.filter(session=session).count()
            rows_dropped += n
            reset += 1
            self.stdout.write(
                f"  reset  {session.id} {session.title[:40]!r} — {n} row(s) -> "
                f"backfill from {binding.runner.name} ({binding.session_key!r})"
            )
            if dry:
                continue
            Message.objects.filter(session=session).delete()
            session.metadata = {**(session.metadata or {}), services.TRANSCRIPT_SOURCED: True}
            session.save(update_fields=["metadata", "updated_at"])
            services.request_backfill(session)

        verb = "would reset" if dry else "reset"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {reset} session(s), dropping {rows_dropped} derived row(s); "
            f"{skipped} skipped ({pruned} pruned). Turns and their ledger untouched."
        ))
        if dry:
            self.stdout.write("(dry run — nothing was changed)")
