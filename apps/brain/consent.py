"""A person's agreement to canopy learning: the only code that writes `LearningConsent` and
`SourceExclusion`.

Every act here is the person's own, except `admin_opt_in`: a workspace's owner or admin (a person, or an admin
agent working for one; `api_manage.opt_people_in`) may ADD classes to a member's consent there. Taking a class away,
narrowing, revoking and withdrawing a session stay the person's alone. The person's routes (`api.py`) refuse an agent's
login, a turn's token, a runner and a site's token before calling in (`tests/test_brain_consent_api.py`). Each change is on the
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

from . import cascade
from .models import CLASSES, INDIVIDUAL, PROJECT, BrainSource, LearningConsent, SourceExclusion

logger = logging.getLogger(__name__)

SOURCE = "brain.consent"
CONSENTED, CLASS_ADDED, CLASS_REMOVED = "learning.consented", "learning.class_added", "learning.class_removed"
REVOKED, SOURCE_WITHDRAWN, SOURCE_RESTORED = "learning.revoked", "learning.source_withdrawn", "learning.source_restored"

#: What each class means to the person, in the words the consent page shows. `seen_by` is who sees
#: the outputs: the contract, not marketing.
CLASS_COPY = {
    "individual": {"learns_about": "you",
                   "summary": "Coaches learn how you work and suggest how to work better. "
                              "To do that, your coaches read the full transcripts of your sessions",
                   "seen_by": "you; your coaches, who read your transcripts; your manager, who sees only "
                              "what was learned, never a transcript"},
    "memory": {"learns_about": "you",
               "summary": "Agents remember what you tell them about yourself and your work, in your memory",
               "seen_by": "only the agents you grant your memory to"},
    "project": {"learns_about": "a project",
                "summary": "Your sessions tied to a project help it move forward. A session shared with "
                           "others is used only if everyone in it agreed",
                "seen_by": "anyone with access to the project, who may read the session with secrets removed"},
    "system": {"learns_about": "canopy and the fleet",
               "summary": "Canopy learns where its agents and tools fail, to fix them",
               "seen_by": "canopy's learning systems and the admins who run them"},
}


class RefusedError(ValueError):
    """The change asked for is not one a person may make (a 422 at the route)."""


def live(user) -> LearningConsent | None:
    return LearningConsent.objects.filter(user=user, revoked_at__isnull=True).first()


def _person(user):
    from apps.contacts import services as contact_services  # noqa: PLC0415 — contacts imports this module

    return contact_services.person_for(user=user)


def _record(user, event_type: str, *, purpose: str, detail: str = "", ids: dict | None = None,
            workspaces=None) -> None:
    """One event on the person's own log and one ids-only row in each workspace they are in, or only in
    `workspaces` (a session's own, for an act about one session: another organisation's log never names it)."""
    from apps.contacts import hcp  # noqa: PLC0415 — contacts imports this module

    person = _person(user)
    if person is not None:
        hcp.audit(person, event_type, actor=hcp.user_actor(user), purpose=purpose, detail=detail)
    payload = {"user_id": user.pk, **(ids or {})}
    for slug in sorted(wsvc.user_workspace_slugs(user) if workspaces is None else set(workspaces)):
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
            raise RefusedError("a standing rule names the learner, purpose, class and/or workspace it denies, each a word")
        if r.get("class") and r["class"] not in CLASSES:
            raise RefusedError(f"unknown class {r['class']!r}")
        out.append(dict(r))
    return out


def _clean_directives(directives) -> list[str]:
    if not isinstance(directives, list) or len(directives) > 20 or not all(
            isinstance(d, str) and 0 < len(d) <= 500 for d in directives):
        raise RefusedError("directives must be a list of at most 20 sentences of at most 500 characters")
    return list(directives)


def _scope(row, workspaces, include_new, mine) -> dict:
    """The scope fields to write. Nothing sent keeps what the row has (a class toggle never moves the
    scope, nor what counts as a "new" workspace). Sent workspaces the person has since left drop out;
    one they were never in is refused; and a list that drops to nothing after that is refused, since
    an empty list would widen the consent to every workspace."""
    out = {}
    if workspaces is not None:
        asked = set(workspaces)
        had = set(row.workspaces or []) if row else set()
        if asked - mine - had:
            raise RefusedError("workspaces may name only workspaces you are in")
        kept = sorted(asked & mine)
        if asked and not kept:
            raise RefusedError("you are no longer in any of the workspaces named; name one you are in")
        out["workspaces"] = kept
        out["known_workspaces"] = sorted(mine) if kept else []
    elif row is None:
        out["workspaces"], out["known_workspaces"] = [], []
    if include_new is not None or row is None:
        out["include_new"] = True if include_new is None else bool(include_new)
    return out


def _narrows(before: dict, after: dict) -> bool:
    """Could the change take away a use the consent allowed: a workspace or "new" workspaces no longer
    covered, or a standing rule added. Dropping a class is checked beside this."""
    old_ws, new_ws = set(before["workspaces"]), set(after["workspaces"])
    if new_ws and (not old_ws or old_ws - new_ws):
        return True
    if new_ws and before["include_new"] and not after["include_new"]:
        return True
    if new_ws and set(after["known_workspaces"]) - set(before["known_workspaces"]) and after["include_new"]:
        return True                        # a workspace that was "new" (covered) is now known and not listed
    return any(r not in before["standing_rules"] for r in after["standing_rules"])


def _state(row) -> dict:
    if row is None:
        return {"workspaces": [], "include_new": True, "known_workspaces": [], "standing_rules": []}
    return {"workspaces": list(row.workspaces or []), "include_new": bool(row.include_new),
            "known_workspaces": list(row.known_workspaces or []), "standing_rules": list(row.standing_rules or [])}


@transaction.atomic
def set_consent(user, *, classes, workspaces=None, include_new: bool | None = None, standing_rules=None,
                directives=None) -> LearningConsent:
    """Make or change the person's consent. `classes` is the whole set; each other field is changed
    only when sent (None keeps it). No classes is refused: ending consent is `revoke`. Anything that
    narrows it (a class dropped, fewer workspaces, `include_new` off, a standing rule added) runs the
    removal cascade over the person's sources. Writes `CoachingOptIn` to match the `individual` class."""
    classes = sorted(set(classes))
    if not classes or set(classes) - set(CLASSES):
        raise RefusedError(f"classes must be a non-empty set of {', '.join(CLASSES)}")
    mine = set(wsvc.user_workspace_slugs(user))
    row = live(user)
    before = set(row.classes) if row else set()
    was = _state(row)
    fields = {"classes": classes, **_scope(row, workspaces, include_new, mine)}
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
    if before - set(classes) or (before and _narrows(was, _state(row))):
        cascade.after_commit(cascade.sources_of(user))
    if PROJECT in classes:
        _look_again(user)
    _sync_coaching(user, on=INDIVIDUAL in classes)
    return row


class OptInRefusedError(ValueError):
    """An admin may not opt this person in (the reason is shown per person)."""


@transaction.atomic
def admin_opt_in(user, *, classes, managed: set[str], by: str) -> tuple[LearningConsent, list[str]]:
    """Add `classes` to `user`'s consent on an admin's word (`by`, as the audit names them). Only ever adds: a class
    the person has is kept, their scope, rules and directives are untouched. `managed` is the set of workspaces the
    admin manages learning in: a person with no consent gets one covering only their workspaces in it (no new ones),
    and one whose consent already reaches a workspace outside it is refused (an admin never widens what the person
    agreed to somewhere they do not manage). Returns the row and the classes added."""
    classes = set(classes)
    if not classes or classes - set(CLASSES):
        raise RefusedError(f"classes must be a non-empty set of {', '.join(CLASSES)}")
    mine = set(wsvc.user_workspace_slugs(user))
    row = live(user)
    if row is None:
        scope = sorted(mine & managed)
        if not scope:
            raise OptInRefusedError("not in a workspace you manage")
        row = LearningConsent.objects.create(user=user, classes=sorted(classes), workspaces=scope,
                                             known_workspaces=sorted(mine), include_new=False)
        added = sorted(classes)
        _record(user, CONSENTED, purpose=f"opted in to canopy learning by {by}",
                detail=",".join(added), ids={"classes": added, "by": by})
    else:
        reach = set(row.workspaces or []) or mine
        if reach - managed:
            raise OptInRefusedError("their consent covers workspaces you do not manage: "
                                    + ", ".join(sorted(reach - managed)))
        added = sorted(classes - set(row.classes))
        if not added:
            return row, []
        row.classes = sorted(set(row.classes) | classes)
        row.save(update_fields=["classes"])
        for cls in added:
            _record(user, CLASS_ADDED, purpose=f"opted in to canopy learning for {cls} by {by}", detail=cls,
                    ids={"class": cls, "by": by})
    if PROJECT in row.classes:
        _look_again(user)
    _sync_coaching(user, on=INDIVIDUAL in row.classes)
    return row, added


@transaction.atomic
def revoke(user) -> bool:
    """End all consent at once: every capability's next read of the person's sessions is a no.
    False when they had none. Derived outputs are withdrawn by the cascade (`cascade.py`)."""
    row = live(user)
    if row is None:
        return False
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at"])
    _record(user, REVOKED, purpose="stop all canopy learning from your sessions")
    cascade.after_commit(cascade.sources_of(user))
    _sync_coaching(user, on=False)
    return True


def _look_again(user) -> None:
    """The associator skips a session until everyone in it agreed to project learning: once this person does (or
    widens it), their sessions are looked at again."""
    from .associate import due_again  # noqa: PLC0415 — associate reads this module's neighbours

    due_again(pk__in=cascade.sources_of(user))


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
            cascade.after_commit(cascade.sources_of(user))
        else:
            row.revoked_at = timezone.now()
            row.save(update_fields=["revoked_at"])
            _record(user, REVOKED, purpose="stop all canopy learning from your sessions")
            cascade.after_commit(cascade.sources_of(user))


# ---- one source at a time ------------------------------------------------------------------------

def exclude(user, source: BrainSource) -> bool:
    """The person takes this source out of learning. Only a participant may (the caller checks and
    answers a stranger "not found"). False when already out."""
    with transaction.atomic():
        _, made = SourceExclusion.objects.get_or_create(user=user, source=source)
        if made:
            _record(user, SOURCE_WITHDRAWN, purpose="remove a session from canopy learning",
                    detail=source.ref, ids={"source": source.pk, "ref": source.ref}, workspaces=[source.workspace_id])
            cascade.after_commit([source.pk])
    return made


def include(user, source: BrainSource) -> bool:
    """Undo `exclude`. It restores eligibility only: what the cascade already withdrew stays
    withdrawn (`cascade.py`)."""
    with transaction.atomic():
        n, _ = SourceExclusion.objects.filter(user=user, source=source).delete()
        if n:
            from .associate import due_again  # noqa: PLC0415 — associate reads this module's neighbours

            due_again(pk=source.pk)
            _record(user, SOURCE_RESTORED, purpose="allow a session in canopy learning again",
                    detail=source.ref, ids={"source": source.pk, "ref": source.ref}, workspaces=[source.workspace_id])
    return bool(n)


def is_participant(user, source: BrainSource) -> bool:
    return source.participants.filter(user=user).exists()
