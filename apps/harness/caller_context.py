"""The caller envelope: who asked for a turn, and what canopy knows about them.

Phase 1b of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md` (§5). Phase 1a RECORDED the initiator on every turn; this
DELIVERS it, so the agent answering an email knows who it is answering and how
sure canopy is, instead of re-deriving that from a `From:` header anyone can
write.

**Data, never prompt.** The runner writes this beside the turn and the agent
reads it (and re-reads it mid-turn with the `who_is_asking` MCP tool). It is
never pasted into the prompt: a chat turn's prompt is the person's own words and
becomes the transcript, so anything prepended would read as something they typed.

**Informs, does not enforce.** The envelope is what the agent's judgement runs
on. Enforcement — what a caller may make the agent DO — is a later phase, and
nothing here should be read as a grant.

**`verified` is about THIS message.** It reads the turn's own assurance, never
the contact's best-ever grade: a forged message from an address that once passed
DMARC is still forged.
"""
from __future__ import annotations

from apps.contacts.models import Contact

from . import initiator as who

#: Bump when a field's MEANING changes; adding a field does not need it.
VERSION = 1

#: User assurances that establish the person, not just a claim about them.
#: `dmarc` is a member resolved from a DMARC-aligned email (harness
#: `_member_behind_email`), which is only ever done on THIS message's grade.
_VERIFIED_USER = frozenset({who.SESSION, who.PAT, who.DELEGATED, who.SLACK_LINKED,
                            who.APPROVAL, Contact.AUTH_DMARC, Contact.AUTH_DKIM_ALIGNED})

#: Relationships, strongest first. `admin` arrives with `Agent.admins` (§3).
OWNER, ADMIN, MEMBER, CALLER, SYSTEM = "owner", "admin", "member", "caller", "system"


def _agent_of(turn):
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    return cs.agent if cs is not None and cs.agent_id else None


def _verified(turn) -> bool:
    kind, grade = turn.initiator_kind, turn.initiator_assurance or ""
    if kind == who.USER:
        return grade in _VERIFIED_USER
    if kind == who.CONTACT:
        # Tier 3: the signature is tied to the identity the reader sees, which
        # for a contact means DMARC-aligned (or domain-signed) MAIL and nothing
        # else. A visitor from an embedded site tops out at tier 2 by design and
        # is therefore never verified here — the host vouches for them, and
        # canopy checks the host's signature, not the human. Gate an embedded
        # agent's interface on `contact`, never `contact:verified`.
        rank = Contact.AUTH_RANK.get(grade, 0)
        return rank >= Contact.AUTH_RANK[Contact.TIER_SIGNED_ALIGNED]
    # canopy itself, or another agent: nobody outside asserted anything.
    return kind in (who.SYSTEM, who.AGENT)


def relationship(turn, agent) -> str:
    """Owner, admin, member, caller or system — what this asker IS to the agent."""
    kind = turn.initiator_kind
    if kind in (who.SYSTEM, who.AGENT):
        return SYSTEM
    user = turn.initiator_user if kind == who.USER else None
    if user is None or agent is None:
        return CALLER
    return relationship_for_user(user, agent)


def relationship_for_user(user, agent) -> str:
    """What a canopy USER is to the agent, with no turn in hand (e.g. listing
    the MCP tools they may call)."""
    if agent is None or not getattr(user, "is_authenticated", False):
        return CALLER
    if agent.owner_id == user.pk:
        return OWNER
    is_admin = getattr(agent, "is_admin", None)
    if callable(is_admin) and is_admin(user):
        return ADMIN
    from apps.workspaces import services as wsvc

    return MEMBER if wsvc.is_member(user, agent.workspace_id) else CALLER


def _contact(contact) -> dict | None:
    if contact is None:
        return None
    return {
        "id": contact.pk,
        "email": contact.email,
        "display_name": contact.display_name,
        "source": contact.source,
        # THIS message's grade and the best ever seen, side by side: a drop is
        # the signal worth noticing (see Contact.last_auth_result).
        "this_message_grade": contact.last_auth_result,
        "best_grade": contact.auth_result,
        "notes": contact.notes,
        "attributes": contact.attributes or {},
        "message_count": contact.message_count,
        "first_seen_at": contact.first_seen_at.isoformat() if contact.first_seen_at else None,
        "is_blocked": contact.is_blocked,
    }


def build(turn) -> dict:
    """The envelope for one turn. Pure read; safe to call on every claim."""
    agent = _agent_of(turn)
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    cs = getattr(turn, "chat_session", None)
    return {
        "version": VERSION,
        "turn_id": str(turn.pk),
        "agent": agent.slug if agent is not None else None,
        "who": who.describe(turn),
        "verified": _verified(turn),
        "relationship": relationship(turn, agent),
        "contact": _contact(turn.initiator_contact) if turn.initiator_contact_id else None,
        "conversation": {
            "session_id": str(cs.pk) if cs is not None else None,
            "thread_id": str(ref.get("thread_id") or "") or None,
            "subject": str(ref.get("subject") or "") or None,
        },
        # What the caller invoked and the scope it grants (§4). null means the
        # agent's FULL profile: its owner, an admin, canopy itself, or an agent
        # that has published no interface. Otherwise the runner and the agent's
        # guard confine the session to exactly this.
        "profile": "restricted" if turn.capability else "full",
        # WHY: owner | admin | system | full:<rule> | capability:<name> | no-interface.
        # `full:contact@dimagi.com:verified` is canopy granting domain-wide access —
        # what `canopy caller tier` reads instead of an allowlist in the repo.
        "granted_by": _granted_by(turn, agent),
        "capability": _profile(agent, turn.capability),
    }


def _granted_by(turn, agent) -> str:
    if agent is None:
        return "no-interface"
    from apps.agents.interface import granted_by

    return granted_by(turn, agent)


def _profile(agent, capability: str):
    if agent is None or not capability:
        return None
    from apps.agents.interface import profile

    return profile(agent, capability)
