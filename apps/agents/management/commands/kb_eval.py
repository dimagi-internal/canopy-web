"""How well a workspace project's KB `search` finds the right item, per mode.

    manage.py kb_eval connect/givewell-kb [--golden FILE] [--modes hybrid,lexical,semantic] [--json]

Runs every question of a golden set (default `apps/agents/kb_golden/<ws>--<handle>.json`)
through the same search the project's MCP server answers, in each mode, and reports
recall@5/10/50 (share of questions with a relevant item that deep) and mean
reciprocal rank, per kind of question and overall. Semantic and hybrid need the
project's vectors (`kb_embed`); without them they are lexical, and say so.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.agents import kb_store, kb_vectors

from .kb_embed import project_from

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "kb_golden"


class Command(BaseCommand):
    help = "Score a workspace project's KB search against a golden set, per search mode."

    def add_arguments(self, parser):
        parser.add_argument("project", help="<workspace>/<handle>, e.g. connect/givewell-kb")
        parser.add_argument("--golden", default="", help="golden-set JSON (default: kb_golden/<ws>--<handle>.json)")
        parser.add_argument("--modes", default="lexical,semantic,hybrid")
        parser.add_argument("--json", action="store_true", dest="as_json", help="print the full result as JSON")

    def handle(self, *args, project: str, golden: str, modes: str, as_json: bool = False, **opts):
        from canopy_kb.evaluate import KS, score, table
        from canopy_kb.hybrid import MODES

        target = project_from(project)
        paths = [Path(golden)] if golden else sorted(GOLDEN_DIR.glob(f"{project.replace('/', '--')}*.json"))
        if not paths or not all(p.exists() for p in paths):
            raise CommandError(f"no golden set at {paths or GOLDEN_DIR}")
        questions = [q for p in paths for q in json.loads(p.read_text(encoding="utf-8"))]
        wanted = [m.strip() for m in modes.split(",") if m.strip()]
        if bad := [m for m in wanted if m not in MODES]:
            raise CommandError(f"unknown mode(s) {bad}; have {list(MODES)}")
        semantic = kb_vectors.semantic_for(target)
        if semantic is None or not target.kb_passages.filter(embedding_model=kb_vectors.model()).exists():
            self.stderr.write("no vectors for this project here: semantic and hybrid are lexical")
        # With a reranker configured, each mode is scored without it and with it.
        runs = [(m, None) for m in wanted]
        if (rerank := kb_vectors.reranker()) is not None:
            runs += [(f"{m}+rerank", rerank) for m in wanted]
        kb = kb_store.open_kb(target)
        try:
            results = {label: score(questions, lambda q, m=label.split("+")[0], rr=rr: [r["id"] for r in kb.search(
                q, limit=max(KS), mode=m, semantic=semantic, rerank=rr)]) for label, rr in runs}
        finally:
            kb.db.close()
        if as_json:
            self.stdout.write(json.dumps(results, indent=2))
        else:
            self.stdout.write(table(results))
