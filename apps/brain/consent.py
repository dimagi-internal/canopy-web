"""A person's agreement to canopy learning: the only code that writes `LearningConsent` and
`SourceExclusion`.

Every act here is the person's own. The routes (`api.py`) refuse an agent's login, a turn's
token, a runner and a site's token before calling in; nothing in this module trusts that, and
nothing calls it for anyone else (`tests/test_brain_consent_api.py`). Each change is on the
person's own audit (`learning.*`, `hcp.audit`) and, ids only, in the event log of every
workspace they are in.

`CoachingOptIn` is the `individual` class's older home and is still read by coaching's session
ACL; the two are kept the same in both directions (`mirror_coaching`, `_sync_coaching`) until
coaching reads this table."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.events import services as events
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .models import CLASSES, INDIVIDUAL, BrainSource, LearningConsent, SourceExclusion

logger = logging.getLogger(__name__)

SOURCE = "brain.consent"
CONSENTED, CLASS_ADDED, CLASS_REMOVED = "learning.consented", "learning.class_added", "learning.class_removed"
REVOKED, SOURCE_WITHDRAWN, SOURCE_RESTORED = "learning.revoked", "learning.source_withdrawn", "learning.source_restored"

#: What each class means to the person, in the words the consent page shows. `seen_by` is who sees
#: the outputs: the contract, not marketing.
CLASS_COPY = {
    "individual": {"learns_about": "you",
                   "summary": "Coaches learn how you work and suggest how to work better",
                   "seen_by": "you, your coaches and your manager"},
    "memory": {"learns_about": "you",
               "summary": "Agents remember what you tell them about yourself and your work",
               "seen_by": "agents serving you, under your grant"},
    "project": {"learns_about": "a project",
                "summary": "Your sessions on a project help it move forward; its team can read them",
                "seen_by": "the project's readers"},
    "system": {"learns_about": "canopy and the fleet",
               "summary": "Canopy learns where its agents and tools fail, to fix them",
               "seen_by": "canopy and agent admins"},
}


class RefusedError(ValueError):
    """The change asked for is not one a person may make (a 422 at the route)."""


def live(user) -> LearningConsent | None:
    return LearningConsent.objects.filter(user=user, revoked_at__isnull=True).first()


def _person(user):
    from apps.contacts import services as contact_services  # noqa: PLC0415 — contacts imports this module

    return contact_services.person_for(user=user)


def _record(user, event_type: str, *, purpose: str, detail: str = "", ids: dict | None = None) -> None:
    """One event on the person's own log and one ids-only row in each workspace they are in."""
    from apps.contacts import hcp  # noqa: PLC0415 — contacts imports this module

    person = _person(user)
    if person is not None:
        hcp.audit(person, event_type, actor=hcp.user_actor(user), purpose=purpose, detail=detail)
    payload = {"user_id": user.pk, **(ids or {})}
    for slug in sorted(wsvc.user_workspace_slugs(user)):
        try:
            events.record([{"source": SOURCE, "kind": event_type, "payload": payload}],
                          workspace=Workspace(pk=slug))
        except Exception:  # noqa: BLE001 — as the session lifecycle: a record is never worth a failed act
            logger.exception("could not record %s in %s", event_type, slug)


def validate_rules(rules) -> list[dict]:
    """Deny-only rules naming only `capability`, `purpose`, `class`, `workspace`, each a word, and
    at least one: an empty rule would deny everything, which is revoking, and has its own route."""
    if not isinstance(rules, list) or len(rules) > 50:
        raise RefusedError("standing_rules must be a list of at most 50 rules")
    out = []
    for r in rules:
        if (not isinstance(r, dict) or not r or set(r) - {"capability", "purpose", "class", "workspace"}
                or not all(isinstance(v, str) and v for v in r.values())):
            raise RefusedError("a standing rule names capability, purpose, class and/or workspace, each a word")
        if r.get("class") and r["class"] not in CLASSES:
            raise RefusedError(f"unknown class {r['class']!r}")
        out.append(dict(r))
    return out


def _clean_directives(directives) -> list[str]:
    if not isinstance(directives, list) or len(directives) > 20 or not all(
            isinstance(d, str) and 0 < len(d) <= 500 for d in directives):
        raise RefusedError("directives must be a list of at most 20 sentences of at most 500 characters")
    return list(directives)


@transaction.atomic
def set_consent(user, *, classes, workspaces=(), include_new: bool = True, standing_rules=None,
                directives=None) -> LearningConsent:
    """Make or change the person's consent so it is exactly this. No classes is refused: ending
    consent is `revoke`. Writes `CoachingOptIn` to match the `individual` class."""
    classes = sorted(set(classes))
    if not classes or set(classes) - set(CLASSES):
        raise RefusedError(f"classes must be a non-empty set of {', '.join(CLASSES)}")
    mine = wsvc.user_workspace_slugs(user)
    workspaces = sorted(set(workspaces))
    if set(workspaces) - mine:
        raise RefusedError("workspaces may name only workspaces you are in")
    row = live(user)
    before = set(row.classes) if row else set()
    fields = {"classes": classes, "workspaces": workspaces, "include_new": bool(include_new),
              "known_workspaces": sorted(mine) if workspaces else []}
    if standing_rules is not None:
        fields["standing_rules"] = validate_rules(standing_rules)
    if directives is not None:
        fields["directives"] = _clean_directives(directives)
    if row is None:
        row = LearningConsent.objects.create(user=user, **fields)
        _record(user, CONSENTED, purpose="agree to canopy learning from your sessions",
                detail=",".join(classes), ids={"classes": classes})
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        row.save()
        for cls in sorted(set(classes) - before):
            _record(user, CLASS_ADDED, purpose=f"agree to canopy learning for {cls}", detail=cls,
                    ids={"class": cls})
        for cls in sorted(before - set(classes)):
            _record(user, CLASS_REMOVED, purpose=f"stop canopy learning for {cls}", detail=cls,
                    ids={"class": cls})
    _sync_coaching(user, on=INDIVIDUAL in classes)
    return row


@transaction.atomic
def revoke(user) -> bool:
    """End all consent at once: every capability's next read of the person's sessions is a no.
    False when they had none. Derived outputs are withdrawn by the cascade (P5)."""
    row = live(user)
    if row is None:
        return False
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at"])
    _record(user, REVOKED, purpose="stop all canopy learning from your sessions")
    _sync_coaching(user, on=False)
    return True


def _sync_coaching(user, *, on: bool) -> None:
    """Keep `CoachingOptIn` the same as the `individual` class (coaching's ACL still reads it)."""
    from apps.contacts import session_reviewers  # noqa: PLC0415 — contacts imports this module

    if on == session_reviewers.live(user).exists():
        return
    person = _person(user)
    (session_reviewers.opt_in if on else session_reviewers.opt_out)(person, user=user)


@transaction.atomic
def mirror_coaching(user, *, on: bool) -> None:
    """Coaching's own opt-in or opt-out happened (`session_reviewers`): make `individual` match,
    creating the consent if this is the person's first. Idempotent, and never calls back."""
    row = live(user)
    if on:
        if row is None:
            LearningConsent.objects.create(user=user, classes=[INDIVIDUAL])
            _record(user, CONSENTED, purpose="agree to canopy learning from your sessions",
                    detail=INDIVIDUAL, ids={"classes": [INDIVIDUAL]})
        elif INDIVIDUAL not in row.classes:
            row.classes = sorted({*row.classes, INDIVIDUAL})
            row.save(update_fields=["classes"])
            _record(user, CLASS_ADDED, purpose="agree to canopy learning for individual",
                    detail=INDIVIDUAL, ids={"class": INDIVIDUAL})
    elif row is not None and INDIVIDUAL in row.classes:
        rest = [c for c in row.classes if c != INDIVIDUAL]
        if rest:
            row.classes = rest
            row.save(update_fields=["classes"])
            _record(user, CLASS_REMOVED, purpose="stop canopy learning for individual",
                    detail=INDIVIDUAL, ids={"class": INDIVIDUAL})
        else:
            row.revoked_at = timezone.now()
            row.save(update_fields=["revoked_at"])
            _record(user, REVOKED, purpose="stop all canopy learning from your sessions")


# ---- one source at a time ------------------------------------------------------------------------

def exclude(user, source: BrainSource) -> bool:
    """The person takes this source out of learning. Only a participant may (the caller checks and
    answers a stranger "not found"). False when already out."""
    with transaction.atomic():
        _, made = SourceExclusion.objects.get_or_create(user=user, source=source)
        if made:
            _record(user, SOURCE_WITHDRAWN, purpose="remove a session from canopy learning",
                    detail=source.ref, ids={"source": source.pk, "ref": source.ref})
    return made


def include(user, source: BrainSource) -> bool:
    """Undo `exclude`. It restores eligibility only: what the cascade already withdrew stays
    withdrawn (P5)."""
    with transaction.atomic():
        n, _ = SourceExclusion.objects.filter(user=user, source=source).delete()
        if n:
            _record(user, SOURCE_RESTORED, purpose="allow a session in canopy learning again",
                    detail=source.ref, ids={"source": source.pk, "ref": source.ref})
    return bool(n)


def is_participant(user, source: BrainSource) -> bool:
    return source.participants.filter(user=user).exists()
