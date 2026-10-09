"""The fleet brain (canopy#804): what agents know about a person, and who read it.

Design: hal `docs/proposals/2026-10-07-caller-context-brain.md` §1–3, approved by
Jonathan 2026-10-07 with these calls:

* Q1 — a person's private conversation with an agent may feed FACTS other agents
  read, work-context kinds only. Raw conversations stay exactly as private as
  before; `conversations()` below is the one new read of turn content, and it is
  limited to the turns ONE agent had with the person, for that agent alone.
* Q2 — v1 serves facts only inside the workspace they were written in.
* Q3 — facts are recorded IN the session that learned them, by the agent (HCP
  `addPreference`); canopy-web makes no model calls. (A separate people-digest
  turn did this until 2026-10-09; it was removed.)
* Q5 — members AND contacts, keyed on `Person` (`services.person_for`).

Every rule a caller could get subtly wrong lives here as a function, so the
REST routes, the envelope and the page all apply the same one.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from .models import Contact, Person, PersonAccess, PersonFact

logger = logging.getLogger(__name__)

#: Most entries the turn-start recall puts in the envelope (HCP `maxEntries`;
#: the protocol ceiling is 20). Small on purpose: the agent recalls more itself.
ENVELOPE_FACTS = 8
#: The page a person reads everything held about them on (a web path).
SEE_ALL = "/people/me/"
#: A conversation's prompt is cut here in `conversations()`.
PROMPT_MAX = 4000


class FactError(ValueError):
    """A fact the rules do not allow. The message is safe to show the caller."""


# --- who a person is ---------------------------------------------------------------


def initiator_person(turn) -> Person | None:
    """The human behind a turn, or None when the asker is not a human.

    A canopy USER (including a member resolved from an aligned email, which
    carries a contact too — the account wins, it is who the turn acts for), or a
    CONTACT. canopy itself, another agent and an unidentified asker have none.
    """
    from apps.harness import initiator as who

    from . import services

    kind = turn.initiator_kind
    if kind == who.USER and turn.initiator_user_id:
        if not is_human_account(turn.initiator_user):
            return None
        return services.person_for(user=turn.initiator_user)
    if kind == who.CONTACT and turn.initiator_contact_id:
        return services.person_for(contact=turn.initiator_contact)
    return None


def is_human_account(user) -> bool:
    """A canopy account a PERSON is behind — not an agent's own login (another
    agent dispatching work arrives as one) and not a system account (CloudWatch
    mail with a member's standing). Neither has anything to remember."""
    from apps.workspaces.system_accounts import is_system_user

    if user is None or agent_of_login(user) is not None:
        return False
    return not is_system_user(user)


def display_name(person: Person) -> str:
    user = person.user if person.user_id else None
    if user is not None:
        full = (user.get_full_name() or "").strip()
        if full:
            return full
    name = (Contact.objects.filter(person=person).exclude(display_name="")
            .order_by("-last_seen_at").values_list("display_name", flat=True).first())
    if name:
        return name
    if user is not None and user.email:
        return user.email
    return person.email or f"person-{person.pk}"


def email_of(person: Person) -> str:
    if person.email:
        return person.email
    if person.user_id and person.user.email:
        return person.user.email.lower()
    return (Contact.objects.filter(person=person).exclude(email="")
            .order_by("-last_seen_at").values_list("email", flat=True).first()) or ""


def by_email(address: str) -> Person | None:
    """The person an address names, without creating one."""
    from . import services

    address = services._normalize(address)
    if not address:
        return None
    hit = Person.objects.filter(issuer="", signer="", email=address).first()
    if hit is not None:
        return hit
    user = services.user_for_verified_email(address)
    if user is not None:
        return Person.objects.filter(user=user).first()
    return None


def known_in(person: Person, workspace_slug: str) -> bool:
    """Is this person someone `workspace_slug` deals with?

    The gate under every per-person read: a member of workspace A must not be
    able to learn, by guessing ids or addresses, that canopy knows someone only
    workspace B deals with. Known = a member there, a contact there, someone a
    turn there was initiated by, or already the subject of a fact there.
    """
    from apps.harness.models import Turn
    from apps.workspaces import services as wsvc

    if not workspace_slug:
        return False
    if person.user_id and wsvc.is_member(person.user, workspace_slug):
        return True
    if Contact.objects.filter(person=person, workspace_id=workspace_slug).exists():
        return True
    if PersonFact.objects.filter(person=person, workspace_id=workspace_slug).exists():
        return True
    return Turn.objects.filter(_initiated_by(person)).filter(
        Q(agent__workspace_id=workspace_slug) | Q(chat_session__workspace_id=workspace_slug)
    ).exists()


def _initiated_by(person: Person) -> Q:
    q = Q(initiator_contact__person=person)
    if person.user_id:
        q |= Q(initiator_user_id=person.user_id, initiator_kind="user")
    return q


# --- facts -------------------------------------------------------------------------


def live_facts(person: Person, workspace_slug: str | None = None, *, for_agent: bool = False):
    """Live facts — neither superseded nor retracted — corrections FIRST (they
    must always be honoured), then newest.

    `for_agent` narrows to what HCP lets an agent see (`hcp.servable`): never a
    conflicted, deprecated or expired entry. The person's own view keeps them —
    a quarantined inference is exactly what they need to see to resolve it."""
    from . import hcp

    qs = PersonFact.objects.filter(person=person, superseded_at__isnull=True,
                                   retracted_at__isnull=True)
    if for_agent:
        qs = hcp.servable(qs)
    if workspace_slug is not None:
        qs = qs.filter(workspace_id=workspace_slug)
    return (qs.select_related("project", "workspace")
            .annotate(_correction_first=Case(When(kind=PersonFact.CORRECTION, then=Value(0)),
                                             default=Value(1), output_field=IntegerField()))
            .order_by("_correction_first", "-created_at", "-pk"))


def fact_dict(fact: PersonFact) -> dict:
    project = None
    if fact.project_id:
        # `title` is the contract's word for the project's `name`.
        project = {"id": fact.project_id, "title": fact.project.name,
                   "ext_id": fact.project.ext_id}
    return {
        "id": fact.pk,
        "kind": fact.kind,
        "statement": fact.statement,
        "basis": fact.basis,
        "project": project,
        "instance_ref": fact.instance_ref,
        "created_at": fact.created_at.isoformat() if fact.created_at else None,
        # HCP v1 (apps/contacts/hcp.py), additive.
        "entry_id": f"urn:uuid:{fact.entry_id}",
        "version": fact.version,
        "category": fact.category,
        "dimension": fact.dimension or None,
        "confidence": fact.confidence or None,
        "status": fact.status,
    }


@transaction.atomic
def record_fact(*, person: Person, workspace, kind: str, statement: str,
                basis: str = PersonFact.DECLARED, by_user=None, by_agent=None,
                source_turn=None, project=None, instance_ref: str = "",
                supersedes: PersonFact | None = None,
                source_contact=None, category: str | None = None,
                dimension: str | None = None, confidence: str | None = None,
                provenance_source: str = "", expires_at=None, relationship: str = "",
                metadata: dict | None = None, user_verified: bool = False,
                captured_by: str = "", reason: str = "", actor=None,
                status: str = PersonFact.ACTIVE, audit_event: str | None = None,
                audit_detail: str = "", check_conflicts: bool = True) -> PersonFact:
    """Append a fact — one VERSION of an HCP entry.

    Without `supersedes` it starts a new entry (version 1). With it, the new row
    is the NEXT VERSION of that entry: same `entry_id`, `version` + 1, and the
    old row stops being current. Either way the write is audited on the
    person's log and conflict-checked (HCP 2.6), on every path — the REST and
    MCP routes, `canopy people remember`, a mirrored contact note.

    Raises FactError for an unknown kind or basis, an empty or over-long
    statement, a project from another workspace, a category canopy does not
    hold, a confidence that does not match the basis, or a superseded fact
    that is not this person's in this workspace.
    """
    from . import hcp

    # Before anything else: nothing from a zero-data-retention session is ever written
    # here (hcp.refuse_if_zdr). Every write path — REST, MCP, CLI, the notes mirror —
    # comes through this function, which is why the check lives here and only here.
    hcp.refuse_if_zdr(source_turn=source_turn, by_agent=by_agent)
    kind = (kind or "").strip()
    if kind not in PersonFact.KINDS:
        raise FactError(f"unknown kind {kind!r}; one of {sorted(PersonFact.KINDS)}")
    basis = (basis or PersonFact.DECLARED).strip()
    if basis not in PersonFact.BASES:
        raise FactError(f"unknown basis {basis!r}; one of {sorted(PersonFact.BASES)}")
    statement = " ".join((statement or "").split())
    if not statement:
        raise FactError("a fact needs a statement")
    if len(statement) > PersonFact.STATEMENT_MAX:
        raise FactError(f"a statement is one sentence, at most {PersonFact.STATEMENT_MAX} characters")
    if project is not None and project.agent.workspace_id != workspace.pk:
        raise FactError("that project belongs to another workspace")
    category = (category or "").strip() or (supersedes.category if supersedes is not None
                                             else hcp.KIND_CATEGORY[kind])
    if not hcp.valid_category(category):
        raise FactError(f"category {category!r} is not one canopy holds")
    confidence = (confidence or "").strip().lower()
    if basis == PersonFact.INFERRED and not confidence:
        confidence = "medium"     # the pre-HCP callers never said; HCP callers must
    if basis == PersonFact.INFERRED and confidence not in PersonFact.CONFIDENCES:
        raise FactError("confidence is high, medium or low")
    if basis != PersonFact.INFERRED:
        confidence = ""
    entry_id, version = uuid.uuid4(), 1
    if supersedes is not None:
        old = PersonFact.objects.select_for_update().filter(pk=supersedes.pk).first()
        if old is None or old.person_id != person.pk or old.workspace_id != workspace.pk:
            raise FactError("a fact can only supersede a fact about the same person in the same workspace")
        if not old.is_live:
            raise FactError("that fact is no longer live")
        entry_id, version = old.entry_id, old.version + 1
    if actor is None:
        actor = (hcp.agent_actor(by_agent) if by_agent is not None
                 else hcp.user_actor(by_user) if getattr(by_user, "is_authenticated", False)
                 else hcp.SYSTEM)
    if supersedes is not None:
        # The old row stops being current BEFORE the new one exists, so the
        # (entry_id, version) history never has two current rows.
        PersonFact.objects.filter(pk=supersedes.pk).update(superseded_at=timezone.now())
    fact = PersonFact.objects.create(
        person=person, workspace=workspace, kind=kind, statement=statement, basis=basis,
        project=project, instance_ref=(instance_ref or "").strip()[:300],
        source_turn=source_turn,
        asserted_by_user=None if by_agent is not None else by_user,
        asserted_by_agent=by_agent, supersedes=supersedes, source_contact=source_contact,
        entry_id=entry_id, version=version, category=category,
        dimension=hcp.normalize_dimension(dimension), confidence=confidence, status=status,
        provenance_source=(provenance_source or "")[:200], expires_at=expires_at,
        relationship=(relationship or "")[:80], metadata=dict(metadata or {}),
        user_verified=bool(user_verified), captured_by=(captured_by or actor.id)[:200],
        reason=(reason or "")[:300],
    )
    event = audit_event or ("preference.updated" if supersedes is not None else "preference.created")
    hcp.audit(person, event, actor=actor, entry=fact, turn=source_turn,
              detail=audit_detail or (f"version {version}" + (f"; {reason}" if reason else "")))
    if check_conflicts:
        hcp.detect_conflicts(fact, actor=actor)
    if project is not None:
        # Any fact filed against a project makes its subject a participant (v1.1).
        from apps.agents import participants

        participants.on_fact(fact)
    return fact


def may_retract(user, fact: PersonFact) -> bool:
    """The person themself, an admin of the fact's workspace, or whoever asserted
    it (the user, or the asserting agent's own login)."""
    from apps.workspaces import permissions as perms

    if not getattr(user, "is_authenticated", False):
        return False
    person = fact.person
    if person.user_id and person.user_id == user.pk:
        return True
    if fact.asserted_by_user_id and fact.asserted_by_user_id == user.pk:
        return True
    if fact.asserted_by_agent_id and fact.asserted_by_agent.user_id == user.pk:
        return True
    return perms.can(user, fact.workspace_id, perms.MEMBERS_MANAGE)


def retract(fact: PersonFact, *, by, actor=None, reason: str = "") -> PersonFact:
    """Soft-delete (HCP `deletePreference`, hardDelete false): the entry's
    status becomes `deleted`. It is still held — the person still sees it and
    an export still carries it — but no agent is ever served it again."""
    from . import hcp

    if fact.retracted_at is None:
        updated = PersonFact.objects.filter(pk=fact.pk, retracted_at__isnull=True).update(
            retracted_at=timezone.now(), status=PersonFact.DELETED,
            retracted_by=by if getattr(by, "is_authenticated", False) else None)
        fact.refresh_from_db()
        if updated:
            if actor is None:
                agent = agent_of_login(by)
                actor = (hcp.agent_actor(agent) if agent is not None
                         else hcp.user_actor(by) if getattr(by, "is_authenticated", False)
                         else hcp.SYSTEM)
            hcp.audit(fact.person, "preference.deleted", actor=actor, entry=fact, detail=reason)
    return fact


# --- reads, logged -----------------------------------------------------------------


def log_access(person: Person, *, via: str, workspace_slug: str | None = None,
               reader_user=None, reader_agent=None, turn=None, had_context: bool = False) -> None:
    """Record a read. Never raises: a read is never worth a failed request."""
    try:
        PersonAccess.objects.create(
            person=person, workspace_id=workspace_slug or None, via=via, turn=turn,
            had_context=had_context,
            reader_user=reader_user if getattr(reader_user, "is_authenticated", False) else None,
            reader_agent=reader_agent)
    except Exception:  # noqa: BLE001
        logger.exception("people: could not log a read of person %s", person.pk)


def agent_of_login(user):
    """The agent whose own canopy login `user` is (`Agent.user`), or None."""
    return getattr(user, "agent_identity", None) if user is not None else None


#: What the turn-start recall says it is for (HCP: every read states a purpose).
ENVELOPE_PURPOSE = "answer this person's turn: context about who is asking"


def envelope_block(turn, *, agent, workspace_slug: str | None, reader_user=None) -> dict | None:
    """The envelope v3 `person` block, and one `PersonAccess(via=envelope)`.

    None when the asker is not a human. Facts are those of the turn's agent's
    workspace only (Q2).

    **HCP recall.** The block is no longer "the person's profile": it is ONE
    `searchPreferences` the control plane runs for the agent at turn start —
    under the grant for this client (agent, channel, host), with the turn's own
    message as the query — so the agent starts with the few entries relevant to
    what was asked (at most `ENVELOPE_FACTS`, never padded) and recalls more
    mid-turn with the `hcp_searchPreferences` tool.
    """
    from . import hcp

    person = initiator_person(turn)
    if person is None:
        return None
    grant, facts = None, []
    if workspace_slug and agent is not None:
        channel, host = hcp.client_of_turn(turn)
        grant = hcp.grant_for(person, agent=agent, workspace_slug=workspace_slug,
                              channel=channel, host=host)
        if grant is not None:
            readable = hcp.categories_for(grant, "read")
            if readable:
                facts, _, _ = hcp.search(person, workspace_slug=workspace_slug,
                                         query=turn.prompt or "", categories=readable,
                                         purpose=ENVELOPE_PURPOSE, max_entries=ENVELOPE_FACTS,
                                         grant=grant, actor=hcp.agent_actor(agent), turn=turn)
    # "Did the brain have anything to say", recorded NOW, for the coverage
    # metric (`apps/contacts/coverage.py`).
    had_context = bool(facts)
    log_access(person, via=PersonAccess.VIA_ENVELOPE, workspace_slug=workspace_slug,
               reader_user=reader_user, reader_agent=agent, turn=turn, had_context=had_context)
    return {
        "id": person.pk,
        "display_name": display_name(person),
        "email": email_of(person),
        # The workspace these facts are from, and where `canopy people remember
        # --workspace <slug>` writes (contract addendum, canopy side).
        "workspace": workspace_slug,
        "facts": [fact_dict(f) for f in facts],
        # HCP: the grant this turn read under (None = the person revoked this
        # client, so nothing about them is served), and how to recall more.
        "grant": ({"id": f"urn:uuid:{grant.grant_id}", "client": grant.client_name,
                   "scopes": list(grant.scopes or [])} if grant is not None else None),
        "recall": ({"tool": "hcp_searchPreferences", "turn": str(turn.pk),
                    "categories": hcp.categories_for(grant, "read"),
                    "hint": "Only the entries relevant to the message are above. To recall "
                            "more about this person, call hcp_searchPreferences with a query, "
                            "the categories, a purpose and turn=<this turn id>."}
                   if grant is not None else None),
        # v1.1, additive: the projects (of this workspace's agents) they take
        # part in, most recently active first, at most 5. Not archived ones.
        "projects": envelope_projects(person, workspace_slug),
        "see_all": SEE_ALL,
    }


def envelope_projects(person: Person, workspace_slug: str | None) -> list[dict]:
    from apps.agents import participants

    if not workspace_slug:
        return []
    rows = participants.projects_of(person, workspace_slug, limit=participants.ENVELOPE_PROJECTS,
                                    include_archived=False)
    return [{"id": r.project_id, "ext_id": r.project.ext_id, "name": r.project.name,
             "agent": r.project.agent.slug} for r in rows]


# --- Contact.notes, mirrored into a fact (v1.1) -----------------------------------
#
# `Contact.notes` is free text a human or agent wrote about a correspondent. The
# brain is the one place agents read what canopy knows about a person, so the
# notes are MIRRORED into a single `role` fact (declared, asserted by nobody),
# tagged with `source_contact` — the marker that makes this idempotent. The
# notes field stays; it is simply no longer a second place to look.

#: The `kind` a mirrored note is filed as. Notes are overwhelmingly "who this
#: person is"; classifying free text would be a model call canopy does not make.
NOTES_KIND = PersonFact.ROLE


def notes_statement(notes: str) -> str:
    """The first 500 characters of the notes, flattened to one line."""
    return " ".join((notes or "").split())[:PersonFact.STATEMENT_MAX]


def mirror_contact_notes(contact: Contact, *, by=None) -> PersonFact | None:
    """Make the contact's live mirrored fact say what its notes say.

    * notes empty → the live mirror (if any) is retracted; returns None.
    * same text as the live mirror → nothing changes; returns it.
    * otherwise → a new fact, superseding the previous mirror.
    """
    from . import services

    statement = notes_statement(contact.notes)
    current = (PersonFact.objects.filter(source_contact=contact, superseded_at__isnull=True,
                                         retracted_at__isnull=True)
               .order_by("-created_at", "-pk").first())
    if not statement:
        if current is not None:
            retract(current, by=by)
        return None
    if current is not None and current.statement == statement:
        return current
    person = services.person_for(contact=contact)
    if person is None:
        return None
    if current is not None and (current.person_id != person.pk
                                or current.workspace_id != contact.workspace_id):
        retract(current, by=by)
        current = None
    # ATTESTED, not declared: someone other than the person wrote these notes
    # about them (HCP issuer-attested, 2.2.2).
    from . import hcp

    try:
        return record_fact(person=person, workspace=contact.workspace, kind=NOTES_KIND,
                           statement=statement, basis=PersonFact.ATTESTED,
                           supersedes=current, source_contact=contact,
                           provenance_source=f"integration:contact-notes:{contact.pk}")
    except hcp.ZdrRefused:
        # Notes edited from inside a zero-data-retention session are not mirrored.
        logger.info("people: notes of contact %s not mirrored (zero data retention)", contact.pk)
        return current


def mirror_all_contact_notes() -> dict:
    """Mirror every contact's notes. Idempotent; returns counts."""
    seen = mirrored = 0
    for contact in Contact.objects.exclude(notes="").select_related("workspace").iterator():
        seen += 1
        if mirror_contact_notes(contact) is not None:
            mirrored += 1
    return {"contacts_with_notes": seen, "mirrored": mirrored}


def conversations(person: Person, agent, *, since: dt.datetime | None = None, limit: int = 200):
    """The turns `person` started WITH `agent` — directly or in one of its chats.

    The one new read of turn content the brain adds, and deliberately narrow:
    only that agent's turns, only ones this person initiated. Gating who may
    call it (the agent's own login, or its admins) is the route's job.
    """
    from apps.harness.models import Turn

    from . import hcp

    qs = (Turn.objects.filter(Q(agent=agent) | Q(chat_session__agent=agent))
          .filter(_initiated_by(person)))
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    # A zero-data-retention session's words never become facts, so they are never
    # handed to whatever derives facts from conversations (hcp.is_zdr_turn).
    rows = qs.select_related("chat_session").order_by("-created_at")[:limit]
    return [t for t in rows if not hcp.is_zdr_turn(t)]


def conversation_dict(turn) -> dict:
    return {
        "id": str(turn.pk),
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
        "origin": turn.origin,
        "via": turn.initiator_via,
        "status": turn.status,
        "prompt": (turn.prompt or "")[:PROMPT_MAX],
        "result_note": turn.result_note or "",
        "chat_session_id": str(turn.chat_session_id) if turn.chat_session_id else None,
        "content_purged": turn.content_purged_at is not None,
    }
