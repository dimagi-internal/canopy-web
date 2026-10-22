"""Search and relevance (3.1, 4.4): lexical and hybrid minimization."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from django.db.models import Q

from ..models import Person, PersonFact, PersonGrant
from .core import (
    HELD_CATEGORIES,
    HYBRID_METHOD,
    MAX_ENTRIES,
    MINIMIZATION_METHOD,
    REDACTED_FIELDS,
    Actor,
    audit,
    denied,
    logger,
    malformed,
    valid_category,
)
from .entries import servable
from .grants import categories_for, within_grant

# --- search (3.1, 4.4) -----------------------------------------------------------

_WORD = re.compile(r"[a-z0-9][a-z0-9_'-]{2,}")
_STOP = frozenset("""the and for are but not you your yours with this that these those from have has had was
were what when where which who whom why how can could would should will shall may might must does did
doing about into onto over under than then them they their there here our ours its it's i'm i've i'd
any all some each very just also only more most such own same too out off again once""".split())
#: A person's own corrections are relevant to anything they ask.
ALWAYS_RELEVANT = frozenset({PersonFact.CORRECTION})
#: Role entries are background for anything too, but every agent-written
#: work_context entry is filed as a role (CATEGORY_KIND), so admitting all of them
#: unmatched would hand over the newest work_context entries whatever was asked.
#: At most this many unmatched ones come along, behind every match.
ROLE_FLOOR = 2
# The tunable knobs are settings (HCP_SEMANTIC_FLOOR, HCP_SEMANTIC_WEIGHT,
# HCP_PROJECT_BOOST in config/settings/base.py), set in canopy-web.cfn.yaml. The
# floor was measured on gemini-embedding-001 against live memory (2026-10-18):
# unrelated questions score 0.45-0.55 against every entry, the right entry
# 0.66-0.73. Re-measure (`manage.py hcp_explain`) before changing the model.


@dataclass
class Score:
    """How one candidate scored against a question, and what became of it."""
    fact: PersonFact
    words: list[str]            # shared with the question
    meaning: float | None       # cosine similarity; None when not compared
    project: bool               # from the turn's project
    total: float
    returned: bool = False
    because: str = ""           # why it was returned, or why not (no ';' in it)


def _knobs(floor, weight, boost) -> tuple[float, float, float]:
    from django.conf import settings

    return (settings.HCP_SEMANTIC_FLOOR if floor is None else floor,
            settings.HCP_SEMANTIC_WEIGHT if weight is None else weight,
            settings.HCP_PROJECT_BOOST if boost is None else boost)


def minimization_method() -> str:
    from .. import embeddings

    return HYBRID_METHOD if embeddings.enabled() else MINIMIZATION_METHOD


def _terms(text: str) -> set[str]:
    return {w.strip("'-") for w in _WORD.findall((text or "").lower())} - _STOP


def _shared(fact: PersonFact, query_terms: set[str]) -> list[str]:
    hay = " ".join(filter(None, [fact.statement, fact.instance_ref, fact.dimension.replace("_", " "),
                                 fact.project.name if fact.project_id else ""]))
    return sorted(query_terms & _terms(hay))


def explain(candidates: list[PersonFact], query: str, *, limit: int, project_id: int | None = None,
            floor: float | None = None, weight: float | None = None,
            boost: float | None = None) -> tuple[list[Score], list[Score], dict]:
    """`rank`, showing its work: (the returned Scores in order, every candidate's
    Score, stats). The knobs default to settings; `hcp_explain` overrides them to
    try a value before it is set."""
    import time

    from .. import embeddings

    floor, weight, boost = _knobs(floor, weight, boost)
    terms = _terms(query)
    sims: dict[int, float] = {}
    embed_ms = None
    if embeddings.enabled() and candidates and (query or "").strip():
        t0 = time.monotonic()
        embeddings.fill(candidates)
        q = embeddings.embed([query], purpose="query")
        embed_ms = round((time.monotonic() - t0) * 1000)
        if q is not None:
            sims = {f.pk: embeddings.similarity(q[0], f.embedding)
                    for f in candidates if embeddings.has_current(f)}
    scores, matched, roles = [], [], []
    for f in candidates:
        words = _shared(f, terms)
        on_project = project_id is not None and f.project_id == project_id
        sim = sims.get(f.pk)
        total = len(words) + (boost if on_project else 0.0)
        if sim is not None and sim >= floor:
            total += sim * weight
        sc = Score(f, words, sim, on_project, total)
        scores.append(sc)
        parts = []
        if sim is not None and sim >= floor:
            parts.append(f"meaning {sim:.2f}")
        if words:
            parts.append("words: " + " ".join(words))
        if on_project:
            parts.append("this project")
        if total > 0 or f.kind in ALWAYS_RELEVANT:
            if f.kind == PersonFact.CORRECTION:
                parts.insert(0, "a correction (always included)")
            sc.because = ", ".join(parts)
            matched.append((0 if f.kind == PersonFact.CORRECTION else 1, -total, -f.created_at.timestamp(), sc))
        elif f.kind == PersonFact.ROLE:
            roles.append((-f.created_at.timestamp(), sc))
        else:
            sc.because = (f"meaning {sim:.2f} is under the {floor:.2f} cutoff, no shared words"
                          if sim is not None else "no shared words")
    matched.sort(key=lambda t: t[:3])
    out = [t[3] for t in matched]
    for i, (_, sc) in enumerate(sorted(roles, key=lambda t: t[0])):
        if i < ROLE_FLOOR:
            sc.because = "background: who they are (role)"
            out.append(sc)
        else:
            sc.because = f"a role entry beyond the {ROLE_FLOOR} that come along unmatched"
    for sc in out[limit:]:
        sc.because = f"past the {limit}-entry limit ({sc.because})"
    out = out[:limit]
    for sc in out:
        sc.returned = True
    best = max(sims.values(), default=None)
    return out, scores, {"candidates": len(candidates), "compared": len(sims), "embed_ms": embed_ms,
                         "best_meaning": best, "floor": floor}


def score_rows(returned: list[Score], scores: list[Score]) -> list[dict]:
    """Every candidate as plain data, in the order shown: what came back by its
    place, then the rest closest in meaning first. Ids, never content."""
    place = {id(sc): i + 1 for i, sc in enumerate(returned)}
    rows = sorted(scores, key=lambda sc: (place.get(id(sc), len(place) + 1), -(sc.meaning or 0), -sc.total))
    return [{"entry_id": str(sc.fact.entry_id) if sc.fact.entry_id else None,
             "meaning": None if sc.meaning is None else round(sc.meaning, 3), "words": sc.words,
             "on_project": sc.project, "total": round(sc.total, 2), "position": place.get(id(sc)),
             "because": sc.because} for sc in rows]


def rank(candidates: list[PersonFact], query: str, *, limit: int,
         project_id: int | None = None) -> list[PersonFact]:
    """The `limit` most relevant of `candidates` (already filtered to what the grant
    reaches — filter first, then rank). Corrections lead; then by score: shared
    words, the turn's project, and with embeddings on, closeness in meaning. Ties go
    to the newer entry. Nothing unrelated pads the result (4.4.2)."""
    return [sc.fact for sc in explain(candidates, query, limit=limit, project_id=project_id)[0]]


def search(person: Person, *, workspace_slugs, query: str, categories: list[str],
           purpose: str, max_entries: int, grant: PersonGrant | None, actor: Actor,
           turn=None, project_id: int | None = None) -> tuple[list[PersonFact], list[str], str]:
    """searchPreferences. Returns (entries, categoriesSearched, retrievalId).

    `workspace_slugs`: every workspace to look in (None = entries in no
    workspace), ranked together — so the best entry wins wherever it lives.
    4.4.1 — only categories the grant reads (the person themself: their own
    held categories); an out-of-scope category is a 403, not a silent drop.
    4.4.2 — at most min(maxEntries, 20), never padded. 4.4.3 — ordered by
    relevance (`rank`), method declared by `minimization_method`.
    Every call appends `preference.read` (one per returned entry, or one for
    an empty result), with the stated purpose.
    """
    slugs = list(workspace_slugs)
    if not (purpose or "").strip():
        raise malformed("purpose is required")
    if not isinstance(max_entries, int) or max_entries < 1:
        raise malformed("maxEntries is a positive integer")
    limit = min(max_entries, MAX_ENTRIES)
    wanted = [c.strip() for c in (categories or []) if c and c.strip()]
    if not wanted:
        raise malformed("categories is required")
    if actor.is_person:
        readable = set(HELD_CATEGORIES) | {c for c in wanted if valid_category(c)}
    else:
        readable = set(categories_for(grant, "read")) if grant is not None else set()
    outside = [c for c in wanted if c not in readable]
    if outside:
        raise denied(f"not authorized for {outside}")
    named = [s for s in slugs if s is not None]
    where = Q(workspace_id__in=named)
    if None in slugs:
        where |= Q(workspace__isnull=True)
    qs = servable(PersonFact.objects.filter(where, person=person, category__in=wanted)
                  ).select_related("project", "person", "workspace") if slugs else PersonFact.objects.none()
    candidates = [f for f in qs if actor.is_person or within_grant(f, grant, "read")]
    returned, scores_all, stats = explain(candidates, query, limit=limit, project_id=project_id)
    hits = [sc.fact for sc in returned]
    workspace_slug = slugs[0] if slugs else None
    retrieval = str(uuid.uuid4())
    # The person sees each read's `why` and the question on /people/me (words.ts
    # `readWhy` parses this; `query=` stays last, since a question may hold ';').
    asked = " ".join((query or "").split())[:200]
    detail = f"retrievalId=urn:uuid:{retrieval}; returned={len(hits)}"
    if hits:
        for sc in returned:
            audit(person, "preference.read", actor=actor, entry=sc.fact, purpose=purpose, grant=grant,
                  turn=turn, detail=f"{detail}; why={sc.because}; query={asked}")
    else:
        best = stats["best_meaning"]
        why = (f"nothing matched (closest meaning {best:.2f}, cutoff {stats['floor']:.2f})"
               if best is not None else "nothing matched")
        audit(person, "preference.read", actor=actor, category=",".join(wanted)[:80], purpose=purpose,
              grant=grant, workspace_slug=workspace_slug, turn=turn, detail=f"{detail}; why={why}; query={asked}")
    from django.conf import settings

    from .. import embeddings
    from ..models import PersonSearch

    PersonSearch.objects.create(
        person=person, retrieval_id=retrieval, actor_id=actor.id[:200], actor_type=actor.type, turn=turn,
        purpose=(purpose or "")[:500], query=(query or "")[:4000], method=minimization_method(),
        model=embeddings.model(), floor=stats["floor"], weight=settings.HCP_SEMANTIC_WEIGHT,
        project_boost=settings.HCP_PROJECT_BOOST, embed_ms=stats["embed_ms"],
        entries=score_rows(returned, scores_all))
    best = stats["best_meaning"]
    logger.info("hcp search: person=%s actor=%s method=%s candidates=%d compared=%d returned=%d "
                "best_meaning=%s floor=%.2f embed_ms=%s retrieval=%s",
                person.pk, actor.id, minimization_method(), stats["candidates"], stats["compared"],
                len(hits), f"{best:.2f}" if best is not None else "-", stats["floor"],
                stats["embed_ms"] if stats["embed_ms"] is not None else "-", retrieval)
    return hits, wanted, retrieval


def minimization(actor: Actor) -> dict:
    return {"method": minimization_method(),
            "redactedFields": [] if actor.is_person else list(REDACTED_FIELDS)}


def read_one(fact: PersonFact, *, actor: Actor, grant: PersonGrant | None, purpose: str,
             turn=None) -> None:
    """GET /v1/preferences/{id} (3.3.6): authorized and audited like a search."""
    if not (purpose or "").strip():
        raise malformed("purpose is required")
    audit(fact.person, "preference.read", actor=actor, entry=fact, purpose=purpose, grant=grant,
          turn=turn, detail=f"single entry, version {fact.version}")
