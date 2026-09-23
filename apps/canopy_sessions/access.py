"""Who may see, and who may act in, a chat session. THE authority.

Every surface asks here: the REST list and by-id reads, every REST write, the
chat socket, attachments, the bulk/maintenance tools, and the push that links
to a chat. Nothing else decides. `tests/test_session_acl.py` pins that the
socket and REST agree for every session shape, and that nothing but an
explicit action creates a `SessionParticipant` row.

The rule, whole:

* **Tenant first.** You must be a member of the session's workspace. Always —
  a participant removed from the workspace loses the session with it, the
  same as every other tenant surface in canopy.
* **Then one of four legs** (`visible_session_q`): you created it; you were
  made a participant; it is a runner-discovered emdash session (tenant-visible
  by design); or it is the own thread of an agent you run.
* **Writing** (sending, answering, stopping, archiving, page actions,
  attachments) needs a role of owner or editor. A `viewer` participant reads.
* **Sharing** — adding or removing a participant — is the OWNER's: the
  creator, or for an agent's own thread the agent's admins. Only someone
  already in the workspace can be added; anyone may remove themselves.

A contact is a different principal with a disjoint predicate
(`contact_session_q`) and never reaches these helpers.

History — why one module:

It lives in its own module because the two used to be written by hand at two
sites and disagreed, in both directions:

* **by-id was too loose.** `_session_or_404` gated on workspace membership
  alone, so any co-tenant holding a session UUID could read a conversation the
  list already refused to show them.
* **the list was wrong about *why*.** It granted co-tenant visibility to
  anything with a `RunnerBinding` — but a *web* session acquires a binding the
  moment a runner picks it up, so a private chat silently became co-tenant
  readable as soon as it started running. `origin` is the property that
  actually distinguishes "nobody in-app created this" from "someone did".
* **the socket had a third rule.** `participants.can_access` admitted ANY
  workspace member and auto-joined them as an editor, so one socket open turned
  a chat REST hid from you into a durable participant grant — and REST then
  honoured it. It also made the chat page 404 on its first REST read and work
  on reload (2026-09-23), because the socket, opened in parallel, granted what
  REST had just refused.

This is the same shape of guard `apps/harness/services.py` uses for
claim-vs-schedule after those two hand-written predicates drifted
(`tests/test_claim_schedule_parity.py`): share the predicate, then assert the
two callers agree, so a re-divergence fails CI instead of production.

Runner-DISCOVERED sessions stay visible to the whole tenant deliberately. They
are created with no `created_by` and no participant row (`harness/services.py`
`Session.objects.create(..., origin=ORIGIN_RUNNER)`), so gating them on
ownership or participation would make them unreachable by *everyone* — that is
the entire emdash-discovered-session flow, not an edge case.
"""

from __future__ import annotations

from django.db.models import Q

from .models import Session, SessionParticipant


def visible_session_q(user) -> Q:
    """Sessions `user` may read, *within a workspace they already belong to*.

    Tenancy is a separate and prior gate (`_visible_slugs`) — this narrows
    inside it and must never be used as the only check.

    Four ways in, and no fifth:

    1. you created it;
    2. you were made a participant — `SessionParticipant` is what "multiplayer"
       means here, and its docstring already calls itself "the authority for
       access and role";
    3. it is a runner-discovered session that a runner is actually reporting,
       which has no creator to belong to;
    4. it is a runner-origin session of an agent YOU RUN, meaning an admin in
       `Agent.is_admin`'s sense: its owner, an owner of its workspace, or an
       explicit `AgentAdmin`. An agent's own work with no creator (an email
       thread, an alarm it picked up) belongs to the people operating that
       agent. Without this leg such a session was readable by nobody, and a
       push for it tapped through to a 404 (2026-09-23, a hal alarm thread).
       "Owner" alone was not enough: on labs every agent's `owner` is null, so
       it matched no one. Narrower than leg 3 on purpose: the agent's admins,
       not the whole tenant.

    Leg 3 keeps `runner_binding__isnull=False` alongside the origin check
    rather than dropping it: origin alone would newly expose runner-origin rows
    that no runner has ever bound (e.g. an email-thread session), which is a
    widening nobody asked for. Both conditions together reproduce exactly
    today's runner-session visibility.

    **Known consequence — an orphaned WEB session becomes invisible.**
    `Session.created_by` is `on_delete=SET_NULL`, so deleting a user leaves
    their web sessions with no creator and no participant, and leg 3 does not
    catch them because their origin is `web`. They stay in the database and
    remain reachable through the admin, but they drop out of the API for
    everyone.

    That is deliberate: the alternative rule — leg 3 as
    `Q(created_by__isnull=True) & Q(runner_binding__isnull=False)`, i.e. "nobody
    owns it, so the tenant may see it" — would hand a departed colleague's
    private conversations to every co-tenant the moment they were offboarded.
    Losing them from a list is the cheaper mistake than publishing them. Switch
    that one leg if the trade should go the other way; nothing else depends on
    the choice.
    """
    return (
        Q(created_by=user)
        | Q(participants__user=user)
        | (Q(origin=Session.ORIGIN_RUNNER) & Q(runner_binding__isnull=False))
        | (Q(origin=Session.ORIGIN_RUNNER) & _agent_admin_q(user))
    )


def _agent_admin_q(user) -> Q:
    """Sessions whose agent `user` is an admin of. The same three ways in as
    `Agent.is_admin`; `tests/test_session_acl.py` checks the two agree.

    The workspace-owner leg asks `workspaces.services`, the one module allowed
    to answer "what is this user's role here?". The explicit-grant leg needs
    no membership check of its own, because every caller of this predicate
    applies the tenant gate first."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    from apps.workspaces import services as wsvc

    owned = [slug for slug in wsvc.user_workspace_slugs(user)
             if wsvc.member_role(user, slug) == wsvc.WorkspaceMembership.OWNER]
    return (Q(agent__owner=user)
            | Q(agent__admin_grants__user=user)
            | Q(agent__isnull=False, agent__workspace_id__in=owned))


def contact_session_q(contact) -> Q:
    """Sessions a CONTACT may read: their own, and only their own.

    Deliberately not a leg of `visible_session_q`. The two predicates are
    disjoint by construction — that one reads `created_by` and participation,
    this one reads `contact` — so a contact cannot appear in a user's list and a
    user's conversation cannot appear in a contact's, without either predicate
    having to remember the other exists.

    There is no participation leg and no tenant leg. A contact is not a member
    (`apps/contacts/models.py`), so "everyone in the workspace" is not a set
    they belong to, and a co-tenant notion of visibility would be exactly the
    grant the Contact model exists to withhold.
    """
    return Q(contact=contact)


_WRITE_ROLES = frozenset({SessionParticipant.OWNER, SessionParticipant.EDITOR})


def _tenant_slugs(user) -> set:
    from apps.workspaces import services as wsvc

    return set(wsvc.user_workspace_slugs(user))


def readable_sessions(user, *, workspace_slugs=None):
    """Every session `user` may read — the tenant gate AND the four legs.

    `workspace_slugs` narrows further (a pinned `/api/w/{ws}/` route); it can
    never widen past the caller's own workspaces.
    """
    if not getattr(user, "is_authenticated", False):
        return Session.objects.none()
    slugs = _tenant_slugs(user)
    if workspace_slugs is not None:
        slugs &= set(workspace_slugs)
    return (Session.objects.filter(workspace_id__in=slugs)
            .filter(visible_session_q(user)).distinct())


def can_read(user, session) -> bool:
    if session is None or not getattr(user, "is_authenticated", False):
        return False
    return readable_sessions(user).filter(pk=session.pk).exists()


def role_for(user, session) -> str | None:
    """The caller's EFFECTIVE role in this session, or None if they cannot read it.

    The HIGHER of two sources. The first is what the rule gives you: the creator
    is the owner, the agent's admins own the agent's own thread, and every other
    leg is an editor, because a runner-discovered session and an agent's thread
    are meant to be worked in, not only watched. The second is an explicit
    participant row, which is how someone is made a viewer or an editor of
    somebody else's chat. A row can raise you but never demote what the rule
    already gives you. Old auto-join rows made an agent's owner a mere "editor"
    of their own agent's thread, unable to share it (seen on labs 2026-09-23).
    """
    if not can_read(user, session):
        return None
    ranks = {SessionParticipant.VIEWER: 0, SessionParticipant.EDITOR: 1, SessionParticipant.OWNER: 2}
    row = (SessionParticipant.objects.filter(session=session, user=user)
           .values_list("role", flat=True).first())
    if session.created_by_id == user.pk:
        derived = SessionParticipant.OWNER
    # An agent's own thread has no creator; the agent's admins stand in for one
    # (leg 4), so there is somebody who can share it.
    elif session.created_by_id is None and session.agent_id and session.agent.is_admin(user):
        derived = SessionParticipant.OWNER
    elif session.origin == Session.ORIGIN_RUNNER and session.created_by_id is None:
        derived = SessionParticipant.EDITOR  # runner-discovered: tenant-visible, workable
    else:
        derived = None  # a web chat you did not create: only a row gets you in
    candidates = [r for r in (row, derived) if r in ranks]
    return max(candidates, key=ranks.__getitem__) if candidates else SessionParticipant.VIEWER


def can_share(user, session) -> bool:
    """Only an owner decides who else is in the conversation."""
    return role_for(user, session) == SessionParticipant.OWNER


def can_write(user, session) -> bool:
    return role_for(user, session) in _WRITE_ROLES
