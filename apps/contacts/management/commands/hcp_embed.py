"""Give every served memory entry a vector from the current embedding model.

    manage.py hcp_embed [--dry-run]

Searches fill missing vectors on their own, a batch at a time, so this is only
needed to do it all at once: right after turning embeddings on, or after changing
HCP_EMBEDDINGS_MODEL (rows from the old model count as having none). Exits 1 if
embeddings are off or a batch fails.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.contacts import embeddings, hcp
from apps.contacts.models import PersonFact


class Command(BaseCommand):
    help = "Embed every served memory entry that has no vector from the current model."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count, don't call the model.")

    def handle(self, *args, dry_run: bool = False, **opts):
        if not embeddings.enabled():
            raise CommandError("embeddings are off: set HCP_EMBEDDINGS_MODEL")
        model = embeddings.model()
        todo = (hcp.servable(PersonFact.objects.all()).exclude(embedding_model=model)
                .select_related("project").order_by("pk"))
        total = todo.count()
        self.stdout.write(f"{total} entries need a vector from {model}")
        if dry_run or not total:
            return
        done = 0
        while True:
            batch = list(todo[:embeddings.FILL_PER_SEARCH])
            if not batch:
                break
            filled = embeddings.fill(batch)
            if not filled:
                raise CommandError(f"embedding failed after {done} of {total}; see the log")
            done += filled
            self.stdout.write(f"  {done}/{total}")
        self.stdout.write(self.style.SUCCESS(f"embedded {done} entries"))
