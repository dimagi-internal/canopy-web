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
ledger — canopy's own record of what it ran, not derivable from anything.

A session is only unresettable when there is no pointer to a transcript (no
binding) or no live runner to read one (offline/retired, which is transient). It
is NOT enough that emdash closed or deleted the task: a backfill resolves the
transcript by worktree PATH under ~/.claude/projects and never asks emdash, and
Claude Code never deletes those files — verified 2026-07-26 against tasks absent
from emdash's DB entirely, whose transcripts still resolved with 545 and 607
records. Falling off the session report ends a session's LISTING, not its
recoverability.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Session


class Command(BaseCommand):
    help = "Reset chat state: drop derived Message rows and re-derive from runner transcripts."

    def add_arguments(self, parser):
        parser.add_argument("--session", help="Reset just this session id.")
        parser.add_argument("--workspace", help="Limit to one workspace slug.")
        parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
        parser.add_argument(
            "--prune-ghosts", action="store_true",
            help="Also DELETE runner-discovered sessions with NO binding — they can "
                 "neither be shown nor rebuilt, and the next session report re-creates "
                 "any whose task is still open. Chats a human started are never pruned.",
        )

    def handle(self, *args, **opts):
        sessions = Session.objects.select_related(
            "runner_binding", "runner_binding__runner"
        ).order_by("created_at")
        if opts["session"]:
            sessions = sessions.filter(pk=opts["session"])
            if not sessions.exists():
                raise CommandError(f"no such session: {opts['session']}")
        if opts["workspace"]:
            sessions = sessions.filter(workspace_id=opts["workspace"])

        # The SAME service the REST endpoint calls — a CLI that reimplemented the
        # rule would drift from the button within a release.
        summary = services.reset_sessions(
            sessions, prune_ghosts=opts["prune_ghosts"], dry_run=opts["dry_run"]
        )
        for r in summary["reset"]:
            self.stdout.write(
                f"  reset  {r['session_id']} {r['title'][:40]!r} — "
                f"{r['rows_dropped']} row(s) -> backfill from {r['runner']}"
            )
        for r in summary["skipped"]:
            self.stdout.write(f"  skip   {r['session_id']} {r['title'][:40]!r} — {r['reason']}")
        for r in summary["pruned"]:
            self.stdout.write(f"  prune  {r['session_id']} {r['title'][:40]!r} (no binding)")
        verb = "would reset" if summary["dry_run"] else "reset"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {len(summary['reset'])} session(s), dropping "
            f"{summary['rows_dropped']} derived row(s); {len(summary['skipped'])} skipped, "
            f"{len(summary['pruned'])} pruned. Turns and their ledger untouched."
        ))
        if summary["dry_run"]:
            self.stdout.write("(dry run — nothing was changed)")
