"""A narrative's human name, and its review state in plain words.

One place, because two copies of "the story's first line, cut at 140 chars"
had drifted into the narratives list, the narrative page and the reviews list,
and an H1 like "…registers each one on their phone, with its name, village, a
GPS poi" is what a first-time visitor read as the page's name (canopy-web#1271).
"""
from __future__ import annotations

# Longer than this and a first line is a scene-setting sentence, not a name —
# the same bar the run-summary page already used for its H1.
MAX_TITLE = 70

_GATE_WORDS = {
    "concept_change": "Story review",
    "external_release": "Release review",
    "product_findings": "Findings review",
}
_STATUS_WORDS = {"pending": "awaiting a decision", "resolved": "decided"}


def humanize_slug(slug: str | None) -> str:
    return (slug or "").replace("-", " ").replace("_", " ").strip().title()


def _clip(text: str, limit: int = MAX_TITLE) -> str:
    """Cut at a word boundary and say so — never mid-word."""
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{cut}…"


def narrative_title(request_json: dict | None, narrative_slug: str | None = None) -> str | None:
    """The narrative's name, best source first:

    1. an explicit ``title`` the author gave the narrative;
    2. the story's first line, when it is short enough to be a name;
    3. the narrative slug, humanized ("chlorine-dispenser-walkthroughs" →
       "Chlorine Dispenser Walkthroughs");
    4. the first line or first scene title, clipped at a word boundary.
    """
    rj = request_json if isinstance(request_json, dict) else {}
    explicit = (rj.get("title") or "").strip() if isinstance(rj.get("title"), str) else ""
    if explicit:
        return _clip(explicit)
    narrative = (rj.get("narrative") or "").strip()
    first = narrative.splitlines()[0].strip() if narrative else ""
    if first and len(first) <= MAX_TITLE:
        return first
    if narrative_slug and narrative_slug.strip():
        return humanize_slug(narrative_slug)
    if first:
        return _clip(first)
    narration = rj.get("narration") or []
    if narration and isinstance(narration[0], dict):
        t = (narration[0].get("title") or "").strip()
        return _clip(t) if t else None
    return None


def phase_words(gate: str | None, status: str | None) -> str:
    """'Story review · awaiting a decision', not 'concept_change · pending'."""
    g = _GATE_WORDS.get(gate or "", humanize_slug(gate).capitalize() if gate else "Review")
    s = _STATUS_WORDS.get(status or "", status or "")
    return f"{g} · {s}" if s else g
