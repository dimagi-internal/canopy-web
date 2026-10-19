"""Give every passage of a workspace project's published KB a vector.

    manage.py kb_embed connect/givewell-kb [--dry-run] [--workers N] [--per-minute N]

A publish syncs on its own in the background, but only up to KB_SYNC_MAX_EMBED
passages; a KB's first publish (or a change of KB_EMBEDDINGS_MODEL) is the whole
file — for the GiveWell KB ~140k Vertex calls, ~$8 — so it is run on purpose with
this. Only new or changed passages are embedded; rerunning after an interruption
resumes. `--dry-run` reports the diff without calling the model.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.agents import kb_store, kb_vectors, services


def project_from(ref: str):
    workspace, _, handle = (ref or "").partition("/")
    project = services.get_workspace_project(workspace, handle) if handle else None
    if project is None:
        raise CommandError(f"no workspace project {ref!r} (name it <workspace>/<handle>)")
    if not project.kb_sha256:
        raise CommandError(f"{ref} has not published a knowledge base")
    return project


class Command(BaseCommand):
    help = "Embed the passages of a workspace project's knowledge base that have no current vector."

    def add_arguments(self, parser):
        parser.add_argument("project", help="<workspace>/<handle>, e.g. connect/givewell-kb")
        parser.add_argument("--dry-run", action="store_true", help="Count, don't call the model.")
        parser.add_argument("--workers", type=int, default=None, help="parallel calls (KB_EMBED_WORKERS)")
        parser.add_argument("--per-minute", type=int, default=None, help="call ceiling (KB_EMBED_PER_MINUTE)")

    def handle(self, *args, project: str, dry_run: bool = False, workers=None, per_minute=None, **opts):
        target = project_from(project)
        if not kb_vectors.available():
            raise CommandError("KB vectors are off here: needs PostgreSQL with the vector extension "
                               "and a vertex/ KB_EMBEDDINGS_MODEL")

        def report(out):
            self.stdout.write(f"  embedded {out['embedded']}  failed {out['failed']}")

        try:
            out = kb_vectors.sync(target, dry_run=dry_run, workers=workers, per_minute=per_minute,
                                  progress=report)
        except (RuntimeError, kb_store.KbPublishError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(str(out))
        if out.get("failed"):
            raise CommandError(f"{out['failed']} passages failed to embed; rerun to retry them")
        self.stdout.write(self.style.SUCCESS(f"{project}: vectors in step"))
