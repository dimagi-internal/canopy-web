"""Show how search scores every one of a person's entries against a question.

    manage.py hcp_explain --person jjackson@dimagi.com "who runs the agents?"
    manage.py hcp_explain --person ... "..." --floor 0.55 --weight 3 --project 12

Each entry's shared words, closeness in meaning, project bonus and total, whether
it would be returned and why (or why not). The knobs default to the live settings
(HCP_SEMANTIC_FLOOR, HCP_SEMANTIC_WEIGHT, HCP_PROJECT_BOOST); pass --floor /
--weight / --boost to try a value before setting it in canopy-web.cfn.yaml.
Read-only, except that the read lands in the person's own audit ("Canopy read your
memory"), like any other read. Guide: docs/architecture/hcp-service.md § Seeing
and tuning search.
"""
from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.contacts import embeddings, hcp
from apps.contacts.models import Person, PersonFact


class Command(BaseCommand):
    help = "Score every one of a person's memory entries against a question, and say why."

    def add_arguments(self, parser):
        parser.add_argument("query")
        parser.add_argument("--person", required=True, help="The person's email address.")
        parser.add_argument("--project", type=int, default=None, help="Score as a turn on this project id.")
        parser.add_argument("--limit", type=int, default=8)
        parser.add_argument("--floor", type=float, default=None)
        parser.add_argument("--weight", type=float, default=None)
        parser.add_argument("--boost", type=float, default=None)

    def handle(self, *args, query, person, project, limit, floor, weight, boost, **opts):
        p = (Person.objects.filter(user__email__iexact=person).first()
             or Person.objects.filter(email__iexact=person).first())
        if p is None:
            raise CommandError(f"no person with email {person}")
        facts = list(hcp.servable(PersonFact.objects.filter(person=p))
                     .select_related("project", "person", "workspace"))
        returned, scores, stats = hcp.explain(facts, query, limit=limit, project_id=project,
                                              floor=floor, weight=weight, boost=boost)
        hcp.audit(p, "preference.read", actor=hcp.SYSTEM,
                  purpose="check how search ranks your memory (manage.py hcp_explain)",
                  detail=f"retrievalId=urn:uuid:{uuid.uuid4()}; returned={len(returned)}; "
                         f"why=scored every entry to tune search; query={' '.join(query.split())[:200]}")
        f, w, b = hcp._knobs(floor, weight, boost)
        self.stdout.write(f"method: {hcp.minimization_method()} ({embeddings.model() or 'no embedding model'})")
        self.stdout.write(f"knobs: floor {f:.2f}  weight {w:g}  project boost {b:g}  limit {limit}")
        self.stdout.write(f"{stats['candidates']} entries, {stats['compared']} compared by meaning"
                          + (f" in {stats['embed_ms']} ms" if stats["embed_ms"] is not None else ""))
        order = {id(sc): i for i, sc in enumerate(returned)}
        rows = sorted(scores, key=lambda sc: (order.get(id(sc), len(order)), -(sc.meaning or 0), -sc.total))
        self.stdout.write("")
        self.stdout.write(f"{'':3} {'total':>5} {'meaning':>7} {'words':>5}  entry")
        for sc in rows:
            mark = f"{order[id(sc)] + 1:>2}." if sc.returned else "  -"
            meaning = f"{sc.meaning:.2f}" if sc.meaning is not None else "-"
            self.stdout.write(f"{mark} {sc.total:5.2f} {meaning:>7} {len(sc.words):>5}  "
                              f"#{sc.fact.pk} {sc.fact.statement[:70]}")
            self.stdout.write(f"{'':25}{sc.because}")
