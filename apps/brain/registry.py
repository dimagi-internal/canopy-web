"""Registering what learns (`BrainCapability`): by a canopy admin only, in one purpose class.

A learning feature's audience is derived from its class and may only NARROW it: a wider audience
than the class allows is refused, because that is the change that needs each person to agree again,
which is a new class (`consent.CLASS_COPY`), not a setting. Its `scope` (how much of a session it reads, a
view of `transcript_read.read`) starts at the whole session and may likewise only narrow; each change is an event. A feature with `enabled` false is
registered but reads nothing (`eligibility.eligible`).

A run of a feature is canopy's own turn, accountable to a person (`runs.start`): the canopy admin who
registered it, or the one who enabled it when nobody had (a seeded learner). An enabled feature always has
one, active; enabling one that would not is refused."""
from __future__ import annotations

import logging

from django.db import transaction

from apps.canopy_sessions import transcript_read
from apps.events import services as events
from apps.harness import invocations
from apps.workspaces import services as wsvc

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


logger = logging.getLogger(__name__)

class RefusedError(ValueError):
    """The registration asked for is not one a canopy admin may make (a 422 at the route)."""


def shown_to(user, caps) -> set[int]:
    """Of `caps`, the learners whose operator, entry and accountable admin `user` may see: a canopy admin sees every
    one; anyone else only one whose operator lives in their own tenant tree (an organisation never learns which agent
    or person runs another's learning), or, with no operator, one whose accountable admin is canopy's or theirs. Everything else about
    a learner (its class, purposes, audience) is what people agree to and is shown to everyone."""
    caps = list(caps)
    if getattr(user, "is_superuser", False):
        return {c.pk for c in caps}
    if not getattr(user, "is_authenticated", False):
        return set()
    mine = set(wsvc.tree_roots(wsvc.user_workspace_slugs(user)).values())
    homes = {c.operator.workspace_id for c in caps if c.operator_id}
    roots = wsvc.tree_roots(homes) if homes else {}
    answer: dict[int, bool] = {}

    def theirs(registrant) -> bool:
        """With no operator, whoever answers for it: canopy's own admin, or someone of the viewer's organisation."""
        if registrant is None:
            return True
        if registrant.pk not in answer:
            answer[registrant.pk] = registrant.is_superuser or bool(
                set(wsvc.tree_roots(wsvc.user_workspace_slugs(registrant)).values()) & mine)
        return answer[registrant.pk]

    return {c.pk for c in caps if (roots.get(c.operator.workspace_id) in mine if c.operator_id
                                   else theirs(c.registered_by))}


def accountable(cap: BrainCapability):
    """The person a run of `cap` acts for (its caller token's user), or None: who registered it, while active."""
    user = cap.registered_by if cap.registered_by_id else None
    return user if user is not None and user.is_active else None


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


def _scope(scope: str) -> str:
    if scope not in transcript_read.VIEWS:
        raise RefusedError(f"scope must be one of {', '.join(transcript_read.VIEWS)}")
    return scope


def _record_scope(cap: BrainCapability, was: str, by) -> None:
    """A learner's scope changed: an event, ids and view names only, on its operator's workspace."""
    workspace = cap.operator.workspace if cap.operator_id else None
    if workspace is None:
        return
    try:
        events.record([{"source": "brain.capability", "kind": "capability.scope_changed",
                        "payload": {"capability": cap.slug, "from": was, "to": cap.scope,
                                    "by_id": getattr(by, "pk", None)}}], workspace=workspace)
    except Exception:  # noqa: BLE001 — a record is never worth a failed change
        logger.exception("could not record the scope change of %s", cap.slug)


def register(*, slug: str, name: str, purpose_class: str, purposes, description: str = "",
             source_kinds=(BrainSource.SESSION,), output_kinds=(), audience=None, operator=None,
             enabled: bool = False, entry: str = "", registered_by=None,
             scope: str = transcript_read.FULL) -> BrainCapability:
    if purpose_class not in CLASS_AUDIENCE:
        raise RefusedError(f"purpose_class must be one of {', '.join(CLASS_AUDIENCE)}")
    _scope(scope)
    if BrainCapability.objects.filter(slug=slug).exists():
        raise RefusedError(f"{slug} is already registered")
    _entry(entry)
    if enabled and not getattr(registered_by, "is_active", False):
        raise RefusedError("an enabled feature runs accountable to an active canopy admin; name one")
    kinds = _words("source_kinds", list(source_kinds))
    if set(kinds) - {k for k, _ in BrainSource.KIND_CHOICES}:
        raise RefusedError("source_kinds names a kind of source canopy does not hold")
    return BrainCapability.objects.create(
        slug=slug, name=name[:120], description=description, purpose_class=purpose_class,
        subject_type=CLASS_SUBJECT[purpose_class], purposes=_words("purposes", list(purposes)),
        source_kinds=kinds, output_kinds=_words("output_kinds", list(output_kinds)),
        audience=_audience(purpose_class, audience), operator=operator,
        entry=entry.strip(), registered_by=registered_by, enabled=bool(enabled), scope=scope)


@transaction.atomic
def update(cap: BrainCapability, *, by=None, **changes) -> BrainCapability:
    """Change a registration. The class never changes (that is a different feature); the audience may
    only narrow what its class allows. `by` is the canopy admin making the change: a feature left enabled
    with nobody active accountable for it (seeded, or its registrant gone) becomes theirs; with no `by`
    it is refused."""
    if "purpose_class" in changes and changes["purpose_class"] != cap.purpose_class:
        raise RefusedError("a feature's class cannot change; register another")
    if "audience" in changes:
        cap.audience = _audience(cap.purpose_class, changes.pop("audience"))
    was = cap.scope
    if "scope" in changes:
        scope = _scope(changes.pop("scope"))
        if transcript_read.narrower(scope, was) != scope:
            raise RefusedError(f"a learner's scope may only narrow: {was} to {scope} would widen it")
        cap.scope = scope
    for key in ("purposes", "source_kinds", "output_kinds"):
        if key in changes:
            setattr(cap, key, _words(key, changes.pop(key)))
    if "entry" in changes:
        _entry(changes["entry"])
        cap.entry = changes.pop("entry").strip()
    for key in ("name", "description", "enabled", "operator"):
        if key in changes:
            setattr(cap, key, changes.pop(key))
    changes.pop("purpose_class", None)
    if changes:
        raise RefusedError(f"cannot change {', '.join(sorted(changes))}")
    if cap.enabled and accountable(cap) is None:
        if not getattr(by, "is_active", False):
            raise RefusedError("an enabled feature runs accountable to an active canopy admin; nobody is")
        cap.registered_by = by
    cap.save()
    if cap.scope != was:
        _record_scope(cap, was, by)
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


#: What a learner ships set up with, so enabling it is one switch plus a target: its operator agent (by slug) and the
#: plugin command its runs start with. Applied to a learner that has no operator yet, when that agent exists
#: (`apply_presets`; migration `0017_preset_system_learning` does the same); never over a choice someone made.
PRESETS = {"system-learning": {"operator": "ada", "entry": "/canopy:brain-learn"}}


def apply_presets(caps=None, agents=None) -> list[str]:
    """Give each preset learner with no operator its preset operator and entry, when that agent exists. Returns the
    slugs set. `caps`/`agents` are the model classes (a migration passes its historical ones)."""
    from apps.agents.models import Agent  # noqa: PLC0415 — agents reads this app's neighbours

    caps, agents = caps or BrainCapability, agents or Agent
    done = []
    for slug, preset in PRESETS.items():
        agent = agents.objects.filter(slug=preset["operator"]).first()
        if agent is not None and caps.objects.filter(slug=slug, operator__isnull=True).update(
                operator=agent, entry=preset["entry"]):
            done.append(slug)
    return done


def ensure_learners() -> dict[str, BrainCapability]:
    """Register the dark learners that are missing, with their presets; the ones there are left as they are."""
    out = {}
    for spec in LEARNERS:
        cls = spec["purpose_class"]
        cap, _ = BrainCapability.objects.get_or_create(slug=spec["slug"], defaults={
            **{k: v for k, v in spec.items() if k != "slug"}, "subject_type": CLASS_SUBJECT[cls],
            "source_kinds": [BrainSource.SESSION], "audience": list(CLASS_AUDIENCE[cls]), "enabled": False})
        out[spec["slug"]] = cap
    if apply_presets():
        out = {slug: BrainCapability.objects.get(pk=cap.pk) for slug, cap in out.items()}
    return out
