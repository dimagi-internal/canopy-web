"""Import a storyboard from a repo-authored YAML file.

Agents author the arc where the product lives — beside the narratives it
curates — and push it here. Idempotent per ``(workspace, slug)``: re-importing
updates the title, prose and order rather than duplicating, so the file stays
the source and this command stays safe to re-run in CI.

    python manage.py import_storyboard storyboard.yaml --workspace dimagi

    slug: ecf-supply
    title: What the money bought
    lede: From the first purchase order to the child who recovered.
    capability: comment          # read | comment | suggest
    acts:
      - title: Six weeks to a supply base
        prose: Procurement integrity you can show, not assert.
        entries: [procurement-eoi, supplier-registry]
      - title: Where the RUTF is, and who is short
        entries:
          - narrative_slug: command-centre
            pinned_run_id: ""    # normally omitted — the entry FOLLOWS current
"""
from __future__ import annotations

from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.storyboards.models import Act, Entry, Storyboard


class Command(BaseCommand):
    help = "Create or update a storyboard from a YAML file."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the storyboard YAML.")
        parser.add_argument("--workspace", required=True, help="Workspace slug that owns it.")

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.exists():
            raise CommandError(f"no such file: {path}")

        raw = yaml.safe_load(path.read_text()) or {}
        slug = (raw.get("slug") or "").strip()
        if not slug:
            raise CommandError("storyboard YAML needs a `slug`")

        with transaction.atomic():
            board, created = Storyboard.objects.update_or_create(
                workspace_id=opts["workspace"],
                slug=slug,
                defaults={
                    "title": raw.get("title") or slug,
                    "lede": raw.get("lede") or "",
                    "capability": raw.get("capability") or Storyboard.CAP_READ,
                },
            )
            # Wholesale replace: reordering is a rewrite, not a diff, and the
            # file is the source of truth for structure.
            board.acts.all().delete()
            for a_pos, act_raw in enumerate(raw.get("acts") or []):
                act = Act.objects.create(
                    storyboard=board,
                    title=act_raw.get("title") or f"Act {a_pos + 1}",
                    prose=act_raw.get("prose") or "",
                    position=a_pos,
                )
                for e_pos, entry_raw in enumerate(act_raw.get("entries") or []):
                    # A bare string is the common case; a mapping is for the
                    # rare pinned entry.
                    if isinstance(entry_raw, str):
                        narrative_slug, pinned = entry_raw, ""
                    else:
                        narrative_slug = entry_raw.get("narrative_slug") or ""
                        pinned = entry_raw.get("pinned_run_id") or ""
                    if not narrative_slug:
                        raise CommandError(
                            f"act {a_pos + 1} entry {e_pos + 1} has no narrative_slug"
                        )
                    Entry.objects.create(
                        act=act,
                        narrative_slug=narrative_slug,
                        pinned_run_id=pinned,
                        position=e_pos,
                    )

        acts = board.acts.count()
        entries = Entry.objects.filter(act__storyboard=board).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {slug}: {acts} act(s), {entries} entr(ies)"
            )
        )
