"""Registering what learns (`BrainCapability`): by a canopy admin only, in one purpose class.

A learning feature's audience is derived from its class and may only NARROW it: a wider audience
than the class allows is refused, because that is the change that needs each person to agree again,
which is a new class (`consent.CLASS_COPY`), not a setting. A feature with `enabled` false is
registered but reads nothing (`eligibility.eligible`)."""
from __future__ import annotations

from django.db import transaction

from apps.harness import invocations

from .models import INDIVIDUAL, MEMORY, PROJECT, SYSTEM, BrainCapability, BrainSource

#: Who may see what a class's features produce: the widest a feature of that class may be.
CLASS_AUDIENCE = {
    INDIVIDUAL: ("person", "coaches", "manager"),
    MEMORY: ("serving_agents",),
    PROJECT: ("project_readers",),
    SYSTEM: ("canopy_admins", "agent_admins"),
}
#: What each class learns ABOUT.
CLASS_SUBJECT = {INDIVIDUAL: "person", MEMORY: "person", PROJECT: "project", SYSTEM: "system"}


#: How often a scheduled feature runs, per subject (`schedule.py`). Empty means by hand only.
SCHEDULES = {"": None, "hourly": 3600, "daily": 86400, "weekly": 7 * 86400}


class RefusedError(ValueError):
    """The registration asked for is not one a canopy admin may make (a 422 at the route)."""


def _words(name: str, value, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list | tuple) or len(value) > limit or not all(
            isinstance(v, str) and 0 < len(v) <= 64 for v in value):
        raise RefusedError(f"{name} must be a list of at most {limit} short words")
    return sorted(set(value))


def _audience(cls: str, audience) -> list[str]:
    widest = CLASS_AUDIENCE[cls]
    if audience is None:
        return list(widest)
    got = _words("audience", audience, limit=len(widest))
    wider = [a for a in got if a not in widest]
    if wider or not got:
        raise RefusedError(f"a {cls} feature's audience is at most {', '.join(widest)}; it may only narrow it")
    return got


def _entry(entry: str) -> None:
    if entry.strip() and (len(entry) > 300 or "\n" in entry or invocations.parse(entry) is None):
        raise RefusedError("entry must be one plugin command: /<plugin>:<skill> [args]")


def _schedule(schedule: str) -> None:
    if schedule not in SCHEDULES:
        raise RefusedError(f"schedule must be one of {', '.join(repr(s) for s in SCHEDULES)}")


def register(*, slug: str, name: str, purpose_class: str, purposes, description: str = "",
             source_kinds=(BrainSource.SESSION,), output_kinds=(), audience=None, operator=None,
             schedule: str = "", enabled: bool = False, entry: str = "", registered_by=None) -> BrainCapability:
    if purpose_class not in CLASS_AUDIENCE:
        raise RefusedError(f"purpose_class must be one of {', '.join(CLASS_AUDIENCE)}")
    if BrainCapability.objects.filter(slug=slug).exists():
        raise RefusedError(f"{slug} is already registered")
    _entry(entry)
    _schedule(schedule)
    kinds = _words("source_kinds", list(source_kinds))
    if set(kinds) - {k for k, _ in BrainSource.KIND_CHOICES}:
        raise RefusedError("source_kinds names a kind of source canopy does not hold")
    return BrainCapability.objects.create(
        slug=slug, name=name[:120], description=description, purpose_class=purpose_class,
        subject_type=CLASS_SUBJECT[purpose_class], purposes=_words("purposes", list(purposes)),
        source_kinds=kinds, output_kinds=_words("output_kinds", list(output_kinds)),
        audience=_audience(purpose_class, audience), operator=operator, schedule=schedule[:64],
        entry=entry.strip(), registered_by=registered_by, enabled=bool(enabled))


@transaction.atomic
def update(cap: BrainCapability, **changes) -> BrainCapability:
    """Change a registration. The class never changes (that is a different feature); the audience may
    only narrow what its class allows."""
    if "purpose_class" in changes and changes["purpose_class"] != cap.purpose_class:
        raise RefusedError("a feature's class cannot change; register another")
    if "audience" in changes:
        cap.audience = _audience(cap.purpose_class, changes.pop("audience"))
    for key in ("purposes", "source_kinds", "output_kinds"):
        if key in changes:
            setattr(cap, key, _words(key, changes.pop(key)))
    if "schedule" in changes:
        _schedule(changes["schedule"])
    if "entry" in changes:
        _entry(changes["entry"])
        cap.entry = changes.pop("entry").strip()
    for key in ("name", "description", "schedule", "enabled", "operator"):
        if key in changes:
            setattr(cap, key, changes.pop(key))
    changes.pop("purpose_class", None)
    if changes:
        raise RefusedError(f"cannot change {', '.join(sorted(changes))}")
    cap.save()
    return cap


#: The one learner canopy itself needs: what a project's readers read a session under (`readers.project_q`).
#: Migration `0005_builtin_learner` seeds the same row; `ensure_builtin` is for a database built without
#: migrations (tests) and `tests/test_brain_admin_api.py` holds the two equal.
PROJECT_READERS = {
    "slug": "project-readers", "name": "Project readers",
    "description": "A project's own readers read the sessions tied to it (read-only, redacted).",
    "purpose_class": PROJECT, "subject_type": "project", "purposes": ["read"], "source_kinds": ["session"],
    "output_kinds": [], "audience": ["project_readers"], "enabled": True,
}


def ensure_builtin() -> BrainCapability:
    cap, _ = BrainCapability.objects.get_or_create(
        slug=PROJECT_READERS["slug"], defaults={k: v for k, v in PROJECT_READERS.items() if k != "slug"})
    return cap


#: The learners canopy has and will have, registered dark: migration `0007_register_the_learners` seeds the same
#: rows and `tests/test_brain_admin_api.py` holds the two equal. `ensure_learners` is for a database built without
#: migrations (tests).
LEARNERS = [
    {"slug": "coaching", "name": "Individual coaching", "purpose_class": INDIVIDUAL, "purposes": ["coach", "review"],
     "output_kinds": ["coaching_note", "pattern", "practice"],
     "description": "Coaches learn how you work and suggest how to work better."},
    {"slug": "agent-memory", "name": "Personal memory", "purpose_class": MEMORY, "purposes": ["remember"],
     "output_kinds": ["hcp_entry"], "description": "Agents remember what you tell them about yourself and your work."},
    {"slug": "project-link-judge", "name": "Project links", "purpose_class": PROJECT, "purposes": ["judge"],
     "output_kinds": ["link"],
     "description": "Decides which project a session belongs to when its activity alone does not say."},
    {"slug": "project-learning", "name": "Project learning", "purpose_class": PROJECT, "purposes": ["learn"],
     "output_kinds": ["idea", "task", "update_draft", "contributor_page", "risk"],
     "description": "Your sessions on a project help it move forward; its team can read them."},
    {"slug": "system-learning", "name": "System learning", "purpose_class": SYSTEM, "purposes": ["improve"],
     "output_kinds": ["finding"], "description": "Canopy learns where its agents and tools fail, to fix them."},
]


def ensure_learners() -> dict[str, BrainCapability]:
    """Register the dark learners that are missing; the ones there are left as they are."""
    out = {}
    for spec in LEARNERS:
        cls = spec["purpose_class"]
        cap, _ = BrainCapability.objects.get_or_create(slug=spec["slug"], defaults={
            **{k: v for k, v in spec.items() if k != "slug"}, "subject_type": CLASS_SUBJECT[cls],
            "source_kinds": [BrainSource.SESSION], "audience": list(CLASS_AUDIENCE[cls]), "enabled": False})
        out[spec["slug"]] = cap
    return out
