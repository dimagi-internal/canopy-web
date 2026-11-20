"""THE question: may this capability learn from this source, for this purpose?

`eligible` is asked on every request and caches nothing, so a revocation, an exclusion, a
sensitivity flag or a deleted transcript changes the next answer. It FAILS CLOSED: a source it
cannot place, a rule it cannot read, a participant it cannot find is a no.

About a person (`individual`, `memory`): only the subject's consent counts, and the subject
must be a participant. Shared (`project`, `system`): every human participant must permit the
class and none may have excluded the source; a contact in it is a no.
`docs/architecture/brain.md` has the table; `tests/test_brain_eligibility.py` is it, row by row."""
from __future__ import annotations

from dataclasses import dataclass

from apps.workspaces import services as wsvc

from . import participants
from .models import ABOUT_A_PERSON, CLASSES, SHARED, BrainCapability, BrainSource, LearningConsent


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _no(reason: str) -> Eligibility:
    return Eligibility(False, reason)


def rule_denies(rule, *, capability: str, purpose: str, cls: str, workspace: str) -> bool:
    """Does a standing rule deny this use? Every key the rule names must match. A rule that is not
    a dict, or names a key this does not know, denies: it cannot be read, so it is not ignored."""
    if not isinstance(rule, dict):
        return True
    have = {"capability": capability, "purpose": purpose, "class": cls, "workspace": workspace}
    if not rule or set(rule) - set(have):
        return True
    return all(rule[k] == have[k] for k in rule)


def covers(consent: LearningConsent, workspace: str) -> bool:
    """Is a source of `workspace` inside this consent's workspaces."""
    if not consent.workspaces or workspace in consent.workspaces:
        return True
    return bool(consent.include_new) and workspace not in (consent.known_workspaces or [])


def _capability(capability) -> BrainCapability | None:
    if isinstance(capability, BrainCapability):
        return BrainCapability.objects.filter(pk=capability.pk).first()   # never trust an in-hand copy
    return BrainCapability.objects.filter(slug=capability).first()


def _consent_denial(user_id, consent, source, cap, purpose) -> str:
    """Why this person's consent does not allow the use, or ""."""
    if consent is None:
        return f"no_consent:{user_id}"
    if cap.purpose_class not in (consent.classes or []):
        return f"class_not_consented:{user_id}"
    if not covers(consent, source.workspace_id):
        return f"outside_workspaces:{user_id}"
    if any(rule_denies(r, capability=cap.slug, purpose=purpose, cls=cap.purpose_class,
                       workspace=source.workspace_id) for r in (consent.standing_rules or [])):
        return f"standing_rule:{user_id}"
    return ""


def eligible(source: BrainSource, capability, purpose: str, subject=None) -> Eligibility:
    """See the module docstring. `capability` is a `BrainCapability` or its slug; `subject` is the
    person (a user or user id) an `individual`/`memory` use is about."""
    if source is None:
        return _no("no_source")
    source = BrainSource.objects.filter(pk=source.pk).select_related("session").first()
    if source is None:
        return _no("no_source")
    if source.state != BrainSource.OPEN:
        return _no("withdrawn")
    reason = participants.sealed_reason(source.session)
    if reason:
        return _no(f"sealed:{reason}")
    cap = _capability(capability)
    if cap is None or not cap.enabled:
        return _no("capability_not_enabled")
    if cap.purpose_class not in CLASSES or purpose not in (cap.purposes or []):
        return _no("purpose_not_declared")
    people = list(source.participants.all())
    users = [p.user_id for p in people if p.user_id is not None]
    excluded = set(source.exclusions.values_list("user_id", flat=True))
    if cap.purpose_class in ABOUT_A_PERSON:
        who = getattr(subject, "pk", subject)
        if who is None or who not in users:
            return _no("subject_not_a_participant")
        if who in excluded:
            return _no(f"excluded:{who}")
        needed = [who]
    elif cap.purpose_class in SHARED:
        if any(p.contact_id is not None for p in people):
            return _no("contact_in_source")
        if not users:
            return _no("no_human_participant")
        hit = next((u for u in users if u in excluded), None)
        if hit is not None:
            return _no(f"excluded:{hit}")
        needed = users
    else:  # pragma: no cover — CLASSES is closed above
        return _no("unknown_class")
    consents = {c.user_id: c for c in LearningConsent.objects.filter(user_id__in=needed, revoked_at__isnull=True)}
    for uid in needed:
        why = _consent_denial(uid, consents.get(uid), source, cap, purpose)
        if why:
            return _no(why)
        member = _user(uid)
        if member is None or not wsvc.is_member(member, source.workspace_id):
            return _no(f"not_a_member:{uid}")
    return Eligibility(True)


def _user(user_id):
    from django.contrib.auth import get_user_model  # noqa: PLC0415 — as the other lazy imports here

    return get_user_model().objects.filter(pk=user_id).first()
