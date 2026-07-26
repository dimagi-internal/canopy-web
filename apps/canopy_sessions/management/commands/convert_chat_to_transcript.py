"""Move an existing chat onto the transcript as its durable record.

New chats are transcript-sourced from birth (see `services.transcript_sourced`).
One created before the unification can't be switched implicitly: its rows are
numbered by a dense counter (0,1,2…) and transcript rows are numbered by record
ordinal, in the same `turn_index` column — mixing them silently drops history
wherever the two spaces overlap. So converting is an explicit, deliberate act.

It is safe because the transcript is a SUPERSET of what the ledger captured: every
message the projection ever wrote came from a record that is still in the .jsonl,
alongside everything the projection could never see (what you typed straight into
emdash, what the agent wrote after handing the floor back). We drop the dense rows
and ask the bound runner to ship the whole transcript back, ordinal-keyed.

    uv run python manage.py convert_chat_to_transcript <session-id> [--dry-run]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session


class Command(BaseCommand):
    help = "Convert a chat session to use its Claude transcript as the durable record."

    def add_arguments(self, parser):
        parser.add_argument("session_id")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change, touch nothing.")

    def handle(self, *args, **opts):
        try:
            session = Session.objects.get(pk=opts["session_id"])
        except (Session.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(f"no such session: {opts['session_id']}") from exc

        if services.transcript_sourced(session):
            self.stdout.write(f"{session.id}: already transcript-sourced — nothing to do")
            return

        binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
        if binding is None or binding.runner_id is None:
            # No emdash session means no transcript to be the record. Refuse rather
            # than delete rows we could not replace.
            raise CommandError(
                f"{session.id}: no runner binding — there is no transcript to convert to. "
                "Nothing was changed."
            )

        rows = Message.objects.filter(session=session)
        n = rows.count()
        if opts["dry_run"]:
            self.stdout.write(
                f"{session.id}: would drop {n} ledger row(s) and request a backfill "
                f"from runner {binding.runner.name} (task {binding.session_key!r})"
            )
            return

        rows.delete()
        session.metadata = {**(session.metadata or {}), services.TRANSCRIPT_SOURCED: True}
        session.save(update_fields=["metadata", "updated_at"])
        state = services.request_backfill(session)
        self.stdout.write(self.style.SUCCESS(
            f"{session.id}: dropped {n} ledger row(s), flagged transcript-sourced, "
            f"backfill {state} (runner {binding.runner.name}, task {binding.session_key!r})"
        ))
        if state != "requested":
            self.stdout.write(self.style.WARNING(
                "  the runner is not reachable right now — history returns when it reports back"
            ))
