"""THE question: may this capability learn from this source, for this purpose?

`eligible` is asked on every request and caches nothing, so a revocation, an exclusion, a
sensitivity flag or a deleted transcript changes the next answer. It FAILS CLOSED: a source it
cannot place, a rule it cannot read, a participant it cannot find is a no.

About a person (`individual`, `memory`): only the subject's consent counts, and the subject
must be a participant. Shared (`project`, `system`): every human participant must permit the
class and none may have excluded the source; a contact in it is a no.
`eligible` is `explain`, which names the reason, and also judges a purpose class with no capability (what canopy's
automatic layers ask, `everyone_agreed`).
`docs/architecture/brain.md` has the table; `tests/test_brain_eligibility.py` is it, row by row."""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q

from apps.agents.models import Agent
from apps.harness import runner_requirements as rr
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import participants
from .models import (
    ABOUT_A_PERSON,
    BRAIN_RUN_KEY,
    CLASSES,
    INDIVIDUAL,
    SHARED,
    BrainCapability,
    BrainSource,
    LearningConsent,
    SourceExclusion,
    SourceParticipant,
)


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


#: Coaching's own learner (`registry.LEARNERS`) and the purposes it reads for.
COACHING, COACHING_PURPOSES = "coaching", ("coach", "review")


def coaching_allows(consent: LearningConsent | None, workspace: str) -> bool:
    """May a coach read this person's sessions of `workspace` (the `individual` class, the `coaching` learner)?
    No consent row leaves coaching to its own opt-in. Otherwise the consent must cover the workspace and no
    standing rule may deny it for any of coaching's purposes: coaching serves both, so a rule naming either
    closes it (fails closed). A rule naming another learner leaves it alone."""
    if consent is None:
        return True
    if not covers(consent, workspace):
        return False
    return not any(rule_denies(r, capability=COACHING, purpose=p, cls=INDIVIDUAL, workspace=workspace)
                   for r in (consent.standing_rules or []) for p in COACHING_PURPOSES)


def _capability(capability) -> BrainCapability | None:
    if isinstance(capability, BrainCapability):
        return BrainCapability.objects.filter(pk=capability.pk).first()   # never trust an in-hand copy
    return BrainCapability.objects.filter(slug=capability).first()


def _consent_denial(user_id, consent, workspace, cls, *, capability="", purpose="") -> str:
    """Why this person's consent does not allow the use, or ""."""
    if consent is None:
        return f"no_consent:{user_id}"
    if cls not in (consent.classes or []):
        return f"class_not_consented:{user_id}"
    if not covers(consent, workspace):
        return f"outside_workspaces:{user_id}"
    if any(rule_denies(r, capability=capability, purpose=purpose, cls=cls, workspace=workspace)
           for r in (consent.standing_rules or [])):
        return f"standing_rule:{user_id}"
    return ""


@dataclass(frozen=True)
class _Facts:
    """What `explain` judges: a source as stored."""

    session: object
    workspace: str
    open: bool
    stored: tuple
    excluded: frozenset


def _facts(source) -> _Facts | None:
    if source is None:
        return None
    source = BrainSource.objects.filter(pk=source.pk).select_related("session").first()
    if source is None:
        return None
    return _Facts(source.session, source.workspace_id, source.state == BrainSource.OPEN,
                  tuple(source.participants.all()), frozenset(source.exclusions.values_list("user_id", flat=True)))


def explain(source=None, capability=None, purpose: str = "", subject=None, *, cls: str = "") -> Eligibility:
    """THE judgement, with its reason: may `capability` learn from this source for `purpose`, about `subject`?
    `eligible` is this. With no `capability`, the same question for a purpose CLASS (`cls`): what canopy's
    automatic layers ask (`everyone_agreed`); only a standing rule naming nothing but the class or workspace counts
    then. Fails closed."""
    facts = _facts(source)
    if facts is None:
        return _no("no_source")
    if not facts.open:
        return _no("withdrawn")
    reason = participants.sealed_reason(facts.session)
    if reason:
        return _no(f"sealed:{reason}")
    slug = ""
    if capability is not None:
        cap = _capability(capability)
        if cap is None or not cap.enabled:
            return _no("capability_not_enabled")
        if cap.purpose_class not in CLASSES or purpose not in (cap.purposes or []):
            return _no("purpose_not_declared")
        if source.kind not in (cap.source_kinds or []):
            return _no("source_kind_not_declared")
        cls, slug = cap.purpose_class, cap.slug
    elif cls not in CLASSES:
        return _no("unknown_class")
    else:
        purpose = ""
    users, has_contact = _people(facts)
    excluded = facts.excluded
    if cls in ABOUT_A_PERSON:
        who = getattr(subject, "pk", subject)
        if who is None or who not in users:
            return _no("subject_not_a_participant")
        if who in excluded:
            return _no(f"excluded:{who}")
        needed = [who]
    else:
        if has_contact:
            return _no("contact_in_source")
        if not users:
            return _no("no_human_participant")
        if excluded:             # anyone's removal, permanently: still in it or since taken out
            return _no(f"excluded:{min(excluded)}")
        needed = users
    consents = {c.user_id: c for c in LearningConsent.objects.filter(user_id__in=needed, revoked_at__isnull=True)}
    for uid in needed:
        why = _consent_denial(uid, consents.get(uid), facts.workspace, cls, capability=slug, purpose=purpose)
        if why:
            return _no(why)
        member = _user(uid)
        if member is None or not wsvc.is_member(member, facts.workspace):
            return _no(f"not_a_member:{uid}")
    return Eligibility(True)


def eligible(source: BrainSource, capability, purpose: str, subject=None) -> Eligibility:
    """See the module docstring. `capability` is a `BrainCapability` or its slug; `subject` is the
    person (a user or user id) an `individual`/`memory` use is about. `explain`, for a stored source."""
    if source is None:
        return _no("no_source")
    return explain(source, "" if capability is None else capability, purpose, subject)


def _people(facts: _Facts) -> tuple[list[int], bool]:
    """The human users in a source and whether a contact is, from the stored rows AND the session read
    live, so someone who joined since the last refresh counts."""
    stored = facts.stored
    live = participants.people_of(facts.session) if facts.session is not None else []
    users = {p.user_id for p in stored if p.user_id is not None} | {u for _h, u, _c in live if u is not None}
    has_contact = any(p.contact_id is not None for p in stored) or any(c is not None for _h, _u, c in live)
    return sorted(users), has_contact


def everyone_agreed(source: BrainSource, cls: str) -> bool:
    """May canopy's AUTOMATIC layers even look at this source for a shared class (`associate`'s dispatched
    and signals layers, a signature's seed)? Only when it is open and unsealed, no contact is in it, no one
    ever excluded it, and every human in it agreed to `cls` for its workspace and is a member there. No
    capability is involved, so only a standing rule that names nothing but the class or workspace counts.
    `explain` for the class. Fails closed."""
    if getattr(source, "pk", None) is None or cls not in SHARED:
        return False
    return bool(explain(source, cls=cls))


def _user(user_id):
    from django.contrib.auth import get_user_model  # noqa: PLC0415 — as the other lazy imports here

    return get_user_model().objects.filter(pk=user_id).first()


# ---- the same question for many sources at once -------------------------------------------------------

def eligible_sources(capability, purpose: str, subject=None):
    """The `BrainSource`s `eligible` says yes to, as ONE queryset, for a read that lists (the project
    leg, a feature's reach). The structure is SQL; what is not (a consent's classes and workspaces, a
    standing rule, who is a member where) is judged in Python over the people who consented, once, and
    handed to the query as sets: still nothing cached, and `tests/test_brain_eligible_sources.py` holds it
    to `eligible` over a whole world. A subject is a user (or id) for `individual`/`memory`."""
    none = BrainSource.objects.none()
    cap = _capability(capability)
    if (cap is None or not cap.enabled or cap.purpose_class not in CLASSES
            or purpose not in (cap.purposes or [])):
        return none
    qs = (BrainSource.objects.filter(kind__in=cap.source_kinds or [], state=BrainSource.OPEN,
                                     session__isnull=False, session__contact__isnull=True,
                                     session__content_deleted_at__isnull=True)
          .exclude(session__metadata__has_key=rr.METADATA_KEY)
          .exclude(session__metadata__has_key=BRAIN_RUN_KEY))                # `participants.sealed_reason`
    consents = [c for c in LearningConsent.objects.filter(revoked_at__isnull=True)
                if cap.purpose_class in (c.classes or [])]
    if cap.purpose_class in ABOUT_A_PERSON:
        who = getattr(subject, "pk", subject)
        consents = [c for c in consents if c.user_id == who]
        if who is None or not consents:
            return none
    if not consents:
        return none
    workspaces = set(Workspace.objects.values_list("slug", flat=True))      # a handful of rows: no scan of the sources
    users = list(LearningConsent.objects.filter(pk__in=[c.pk for c in consents]).values_list("user_id", flat=True))
    roles = wsvc.member_roles_of(_users(users))
    bad_in: dict[str, set[int]] = {}      # workspace -> people whose consent does not allow a source there
    for c in consents:
        for ws in workspaces:
            denied = (ws not in roles.get(c.user_id, {}) or not covers(c, ws)
                      or any(rule_denies(r, capability=cap.slug, purpose=purpose, cls=cap.purpose_class,
                                         workspace=ws) for r in (c.standing_rules or [])))
            if denied:
                bad_in.setdefault(ws, set()).add(c.user_id)
    consenting = set(users)
    if cap.purpose_class in ABOUT_A_PERSON:
        subject_id = consents[0].user_id
        allowed = [ws for ws in workspaces if subject_id not in bad_in.get(ws, set())]
        mine = SourceParticipant.objects.filter(source=OuterRef("pk"), user_id=subject_id)
        out = SourceExclusion.objects.filter(source=OuterRef("pk"), user_id=subject_id)
        return qs.filter(Exists(mine), ~Exists(out), workspace_id__in=allowed)
    humans = SourceParticipant.objects.filter(source=OuterRef("pk"), user__isnull=False)
    bad = ~Q(user_id__in=consenting)
    for ws, people in bad_in.items():
        bad |= Q(source__workspace_id=ws, user_id__in=people)
    contacts = SourceParticipant.objects.filter(source=OuterRef("pk"), contact__isnull=False)
    out = SourceExclusion.objects.filter(source=OuterRef("pk"))      # anyone's, member or not, for good
    qs = qs.filter(Exists(humans), ~Exists(contacts), ~Exists(out), ~Exists(humans.filter(bad)))
    return _live_people_agree(qs, consenting, bad_in)


def _live_people_agree(qs, consenting: set[int], bad_in: dict[str, set[int]]):
    """The session's own rows, read live, so a person added to it or starting a turn in it since the last
    `sources.refresh` is asked too: no one added (`SessionParticipant`) or who started a turn (`Turn`
    initiator) may be without consent, and a contact starting a turn is a no. An agent's login is never
    asked. Indexed foreign keys only; a line written outside a turn is caught by `refresh`."""
    from apps.canopy_sessions.models import SessionParticipant  # noqa: PLC0415 — as the other lazy imports here
    from apps.harness.models import Turn  # noqa: PLC0415 — as the other lazy imports here

    agents = Agent.objects.filter(user__isnull=False).values("user_id")
    added = SessionParticipant.objects.filter(session=OuterRef("session_id")).exclude(user_id__in=agents)
    by_chat = Turn.objects.filter(chat_session=OuterRef("session_id"))
    by_canopy = Turn.objects.filter(canopy_session=OuterRef("session_id"))

    def started(turns, people):
        return turns.filter(initiator_user_id__in=people)

    def unconsented_starter(turns):
        return turns.filter(Q(initiator_contact__isnull=False)
                            | (Q(initiator_user__isnull=False) & ~Q(initiator_user_id__in=sorted(consenting))
                               & ~Q(initiator_user_id__in=agents)))

    qs = qs.filter(~Exists(added.exclude(user_id__in=sorted(consenting))),
                   ~Exists(unconsented_starter(by_chat)), ~Exists(unconsented_starter(by_canopy)))
    for ws, people in bad_in.items():
        people = sorted(people)
        qs = qs.exclude(Q(workspace_id=ws) & (Exists(added.filter(user_id__in=people))
                                              | Exists(started(by_chat, people)) | Exists(started(by_canopy, people))))
    return qs


def _users(ids):
    from django.contrib.auth import get_user_model  # noqa: PLC0415 — as the other lazy imports here

    return list(get_user_model().objects.filter(pk__in=ids))
