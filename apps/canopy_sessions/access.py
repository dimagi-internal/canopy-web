"""Who may see, and who may act in, a chat session. THE authority.

Every surface asks here: the REST list and by-id reads, every REST write, the
chat socket, attachments, the bulk/maintenance tools, and the push that links
to a chat. Nothing else decides. `tests/test_session_acl.py` pins that the
socket and REST agree for every session shape, and that nothing but an
explicit action creates a `SessionParticipant` row.

The rule, whole — a session is visible to the people who were IN the
conversation where it happened, plus whoever they explicitly add; never to
someone merely for being in the same workspace:

* **Tenant first.** You must be a member of the session's workspace. Always —
  a participant removed from the workspace loses the session with it, the
  same as every other tenant surface in canopy. Which workspace that is — the
  filing rule for a new runner session, and moving one — is `filing.py`.
* **Then one of seven legs** (`visible_session_q`): you created it; you were
  made a participant; it is a runner-discovered session on a box YOU OWN (a
  laptop session is its owner's alone); it is the own thread of an agent you
  run; you are an agent's own login and its person let that agent review
  their sessions (`SessionReviewGrant`, read-only, never a sensitive one); or
  you administer the runner it sits on (`RunnerAdmin`, read-only, so a box
  can be emptied — `may_move`); or its box owner shared their laptop sessions
  in this project's repo with the project's team and you are on it
  (`ProjectSessionShare`, read + write + pull).
* **Writing** (sending, answering, stopping, archiving, page actions,
  attachments) needs a role of owner or editor. A `viewer` participant reads.
* **Sharing** — adding or removing a participant — is the OWNER's: the
  creator, the owner of the box a discovered session runs on, or for an
  agent's own thread the agent's admins. Only someone already in the
  workspace can be added; anyone may remove themselves.
* **Pulling** — asking a runner to stream a session live (`attach`) or to ship
  its history (`backfill`) — is `may_pull`: someone who may write in it, or
  the owner of the box it runs on. Canopy only ever reads what a machine
  already SENT; it reaches down to a laptop only when its owner, or someone
  they explicitly added, asks. A reviewer, a runner admin and any agent login
  read the stored rows and nothing more.

The audience per channel is tabled in `docs/architecture/access.md`
§ Who can see a session.

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

A runner-DISCOVERED session has no `created_by` and no participant row
(`harness/session_sync.py`, `Session.objects.create(..., origin=ORIGIN_RUNNER)`).
It belongs to the person whose box it runs on (`runner_binding__runner__owner`):
their machine, their Claude login, their conversation. Sharing one is an
explicit act — a participant row its owner adds — never a consequence of
being in the same workspace, and never something an agent login inherits.
A box with no owner gives the leg to nobody (it fails closed).
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from apps.agents.models import Agent, AgentProject
from apps.harness import runner_requirements as rr
from apps.harness import runners, sensitivity
from apps.harness.models import Runner

from .models import (
    ProjectSessionShare,
    RunnerBinding,
    Session,
    SessionParticipant,
    SessionReviewGrant,
)


def visible_session_q(user) -> Q:
    """Sessions `user` may read, *within a workspace they already belong to*.

    Tenancy is a separate and prior gate (`_visible_slugs`) — this narrows
    inside it and must never be used as the only check.

    Seven ways in, and no eighth:

    1. you created it;
    2. you were made a participant — `SessionParticipant` is what "multiplayer"
       means here, and its docstring already calls itself "the authority for
       access and role";
    3. it is a runner-discovered session (no creator) on a box YOU OWN
       (`_box_owner_q`): a laptop session is its owner's, not the tenant's.
       An agent's turn on that box is included — a box runs an agent only if
       its owner administers the agent (`runner_may_hold_agent`), so that adds
       no one leg 4 does not already have, and it keeps a person's own emdash
       session in an agent's repo theirs;
    4. it is a runner-origin session of an agent YOU RUN, meaning an admin in
       `Agent.is_admin`'s sense: its owner, an owner of its workspace, or an
       explicit `AgentAdmin`. An agent's own work with no creator (an email
       thread, an alarm it picked up) belongs to the people operating that
       agent. Without this leg such a session was readable by nobody, and a
       push for it tapped through to a 404 (2026-09-23, a hal alarm thread).
       "Owner" alone was not enough: on labs every agent's `owner` is null, so
       it matched no one. The agent's admins, not the whole tenant. The
       agent's OWN login also reads its own work (`_own_work_q`: no creator,
       its agent) — read-only, no pull; never a conversation a person started
       with it, which has a creator.
    5. you are an agent's own login (`Agent.user`) and the session's person let
       THAT agent review their sessions (a live `SessionReviewGrant`, the
       person's own act). Their sessions are the ones they created and the
       runner-discovered sessions on a box they own that are not an agent's own
       work (`sessions_of_q`). Never a sensitive one: a session carrying any
       runner requirement is left out (`_not_sensitive_q`, the SQL form of
       `sensitivity.is_sensitive`, failing closed). Another agent's login, or a
       person who administers the agent, gains nothing from the grant; revoking
       it drops the next read.
    6. you administer the runner it is bound to (`RunnerAdmin`, an explicit
       per-box grant its owner made) and it is a runner-discovered session
       (`_runner_admin_q`). READ-ONLY (`role_for` gives a viewer) and no pull
       (`may_pull`): it exists so the person trusted with a box can see what is
       on it and move it off (`may_move`, `transfer-all`), not to work in it.
    7. its box owner shared their laptop sessions in this project's repo with the
       project's team, and you are on that team (`ProjectSessionShare`,
       `_project_share_q`): the project's owner or a person taking part in it,
       never an agent's own login. The person's explicit, revocable choice, per
       project; you work in it as an editor and may pull. Never a sensitive one,
       never an agent's own work.

    **Known consequence — an orphaned session becomes invisible.**
    `Session.created_by` is `on_delete=SET_NULL`, so deleting a user leaves
    their web sessions with no creator and no participant; a laptop whose owner
    is gone leaves its sessions with no box owner. They stay in the database and
    remain reachable through the admin, but they drop out of the API for
    everyone. Losing them from a list is the cheaper mistake than handing a
    departed colleague's conversations to every co-tenant.
    """
    return (
        Q(created_by=user)
        | Q(participants__user=user)
        | _box_owner_q(user)
        | (Q(origin=Session.ORIGIN_RUNNER) & _agent_admin_q(user))
        | _own_work_q(user)
        | _review_grant_q(user)
        | _runner_admin_q(user)
        | _project_share_q(user)
    )


def _project_share_q(user) -> Q:
    """Leg 7: laptop sessions their box owner shared with a project team `user` is on."""
    if not getattr(user, "is_authenticated", False) or Agent.objects.filter(user=user).exists():
        return Q(pk__in=[])
    on_team = AgentProject.objects.filter(
        Q(owner_user=user) | Q(participant_rows__person__user=user)).values("pk")
    shares = (ProjectSessionShare.objects
              .filter(revoked_at__isnull=True, project__in=on_team)
              .exclude(project__repo_slug="")
              .filter(user_id=OuterRef("runner_binding__runner__owner_id"),
                      project__workspace_id=OuterRef("workspace_id"),
                      project__repo_slug__iexact=OuterRef("runner_binding__emdash_project")))
    return _discovered_q() & Q(agent__isnull=True) & _not_sensitive_q() & Q(Exists(shares))


def is_project_shared_with(user, session) -> bool:
    """Leg 7 for one row already in hand."""
    if session is None or session.origin != Session.ORIGIN_RUNNER or session.created_by_id is not None:
        return False
    return Session.objects.filter(pk=session.pk).filter(_project_share_q(user)).exists()


def _discovered_q() -> Q:
    """A runner-discovered session: runner-origin, nobody in-app created it."""
    return Q(origin=Session.ORIGIN_RUNNER, created_by__isnull=True)


def _own_work_q(user) -> Q:
    """Leg 4's other half: an agent's own login reads the agent's own work."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    return _discovered_q() & Q(agent__isnull=False, agent__user=user)


def _box_owner_q(user) -> Q:
    """Leg 3: runner-discovered sessions on a box `user` owns.

    The binding's live `runner` FK first. A session that has fallen off its
    box's report has that FK cleared, so it is recovered by the binding's `host`
    — the same identity the report loop and `filing._runner_owned_by` recover it
    by (never a blank host, which any un-heartbeated runner shares). Without it a
    laptop's finished sessions would vanish from their own owner."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    hosts = Runner.objects.filter(owner=user).exclude(host="").values("host")
    return _discovered_q() & (
        Q(runner_binding__runner__owner=user)
        | Q(runner_binding__runner__isnull=True, runner_binding__host__in=hosts)
    )


def _runner_admin_q(user) -> Q:
    """Leg 6: runner-discovered sessions on a box `user` was made an admin of."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    return _discovered_q() & Q(runner_binding__runner__admins__user=user)


def is_box_owner(user, session) -> bool:
    """Whether `session` is a runner-discovered session on a box `user` owns —
    leg 3, for one row already in hand."""
    if not getattr(user, "is_authenticated", False):
        return False
    if session.origin != Session.ORIGIN_RUNNER or session.created_by_id is not None:
        return False
    binding = _binding(session)
    if binding is None:
        return False
    if binding.runner_id is not None:
        return binding.runner.owner_id is not None and binding.runner.owner_id == user.pk
    if not binding.host:
        return False
    return Runner.objects.filter(host=binding.host, owner=user).exists()


def _binding(session):
    try:
        return session.runner_binding
    except RunnerBinding.DoesNotExist:
        return None


def sessions_of_q(user_ids) -> Q:
    """A person's OWN sessions, for `user_ids` (ids or a subquery of them): the
    chats they started, and the runner-discovered sessions on a box they own that
    are not an agent's own work. Narrows; grants nothing by itself."""
    return (Q(created_by_id__in=user_ids)
            | Q(origin=Session.ORIGIN_RUNNER, created_by__isnull=True, agent__isnull=True,
                runner_binding__runner__owner_id__in=user_ids))


def _not_sensitive_q() -> Q:
    """Sessions that are certainly not sensitive: no runner requirement at all.
    `sensitivity.is_sensitive` reads the same key; any value there (even one that
    would parse as harmless) is left out, so a new flag fails closed."""
    return ~Q(metadata__has_key=rr.METADATA_KEY)


def _review_grant_q(user) -> Q:
    """Leg 5: the sessions of the people who let the agent whose own login
    `user` is review them. Empty for anyone else."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    granters = (SessionReviewGrant.objects
                .filter(agent__user=user, revoked_at__isnull=True).values("user_id"))
    return sessions_of_q(granters) & Q(contact__isnull=True) & _not_sensitive_q()


def _agent_admin_q(user) -> Q:
    """Sessions whose agent `user` is an admin of. The same three ways in as
    `Agent.is_admin`; `tests/test_session_acl.py` checks the two agree.

    The workspace-owner leg asks `workspaces.services`, the one module allowed
    to answer "what is this user's role here?". The explicit-grant leg needs
    no membership check of its own, because every caller of this predicate
    applies the tenant gate first."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    from apps.workspaces import permissions as perms

    owned = perms.slugs_with(user, perms.OWN)
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
    """Every session `user` may read — the tenant gate AND the seven legs.

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


def readable_ids(user, session_ids) -> set:
    """Which of `session_ids` `user` may read — `can_read` for a whole list, in
    one query. The same predicate (`readable_sessions`), so it cannot disagree
    with `can_read`; it exists because a page of turns asked once per row."""
    ids = {i for i in session_ids if i}
    if not ids or not getattr(user, "is_authenticated", False):
        return set()
    return set(readable_sessions(user).filter(pk__in=ids).values_list("pk", flat=True))


def can_read(user, session) -> bool:
    if session is None or not getattr(user, "is_authenticated", False):
        return False
    return readable_sessions(user).filter(pk=session.pk).exists()


def role_for(user, session) -> str | None:
    """The caller's EFFECTIVE role in this session, or None if they cannot read it.

    The HIGHER of two sources. The first is what the rule gives you: the creator
    is the owner, the agent's admins own the agent's own thread, the owner of
    the box a discovered session runs on owns it, and the read-only legs (a
    coach's review grant, a runner admin) give a viewer. The second is an explicit
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
    elif is_box_owner(user, session):
        # Runner-discovered, on YOUR box: your laptop, your conversation. You
        # work in it and decide who else is in it.
        derived = SessionParticipant.OWNER
    elif is_project_shared_with(user, session):
        # Its box owner shared their sessions in this project's repo with the
        # project's team, and you are on it: you work in it with them.
        derived = SessionParticipant.EDITOR
    elif session.origin == Session.ORIGIN_RUNNER and session.created_by_id is None:
        # Seen through a read-only leg (a coach's review grant, a runner admin,
        # an agent's own login on its own work):
        # they read what is stored. Typing into it would be typing into somebody
        # else's live session; a participant row is the only way to be let in.
        derived = SessionParticipant.VIEWER
    else:
        derived = None  # a web chat you did not create: only a row gets you in
    if derived is not None and row is None and session.created_by_id != user.pk:
        # A SENSITIVE conversation's words are sealed to its own people: a leg that
        # lets someone see it exists (an agent's admin, the owner of the box a
        # discovered one runs on) does not make them able to drive, share or copy
        # it — they read its metadata. Only being in it does
        # (`sensitivity.is_conversation_person`: its creator, or a participant row).
        if sensitivity.is_sensitive(session):
            derived = SessionParticipant.VIEWER
    candidates = [r for r in (row, derived) if r in ranks]
    return max(candidates, key=ranks.__getitem__) if candidates else SessionParticipant.VIEWER


def can_share(user, session) -> bool:
    """Only an owner decides who else is in the conversation."""
    return role_for(user, session) == SessionParticipant.OWNER


def can_write(user, session) -> bool:
    return role_for(user, session) in _WRITE_ROLES


#: What a caller who may read a session but not pull from its runner is told.
PULL_REFUSED = ("Only the person whose machine this session runs on, or someone they "
                "added to it, can ask that machine for more. You can read what it has "
                "already sent.")


def may_pull(user, session) -> bool:
    """May `user` make canopy ask the session's runner for something — stream it
    live (`attach`) or ship its history (`backfill`)?

    Canopy only ever operates on what a machine already SENT; it reaches down to
    a box only when its owner, or someone they explicitly let in, asks. So: the
    owner of the box a discovered session runs on (even of a sensitive one, whose
    words still go only to its own people), and anyone the rule lets write in it
    — its creator, an agent's admins for the agent's own thread, a participant
    added as owner or editor. A coach reading under a review grant, a runner
    admin, a viewer, and any agent login not explicitly added read the stored
    rows and nothing more."""
    if not getattr(user, "is_authenticated", False) or not can_read(user, session):
        return False
    return is_box_owner(user, session) or can_write(user, session)


def may_move(user, session) -> bool:
    """May `user` move this session to another runner (`transfer`, `transfer-all`)?
    Anyone who may write in it, or an admin of the box it is on — the person
    trusted with emptying that box. Where it may land is the transfer's own rule
    (`transfer_requests.request`)."""
    if not can_read(user, session):
        return False
    if can_write(user, session):
        return True
    binding = _binding(session)
    if binding is None or binding.runner_id is None:
        return False
    return runners.can_administer_runner(user, binding.runner)


def own_turn_session(user, turn_id):
    """The ONE conversation an agent's own login may act on without reading it:
    the session of a live turn it is running. None otherwise.

    An agent's own login (`Agent.user`) is not a leg of `visible_session_q`: a
    Slack thread a person started belongs to that person, so the agent answering
    it cannot open it, list it, or name it by id (and must not — that would hand
    it every conversation it ever had). But the agent IS having this
    conversation, and moving it to a new Slack thread or detaching it is part of
    having it (2026-10-16, an ACE turn told to "post a new clean thread" that
    404'd on its own session). So: named by `turn_id`, and only while that turn
    is in flight (claimed, running or waiting on a person), only for the login
    of the agent the turn runs AS, and only inside a workspace that login
    belongs to. Grants nothing to read; the callers decide what acting means
    (`share.share_session`, `share.detach_session`, `share.start_new_thread`).
    """
    import uuid

    from apps.harness.models import Turn

    if not getattr(user, "is_authenticated", False) or not turn_id:
        return None
    try:
        pk = uuid.UUID(str(turn_id))
    except ValueError:
        return None
    turn = (Turn.objects.select_related("chat_session", "chat_session__agent", "agent")
            .filter(pk=pk, chat_session__isnull=False,
                    status__in=list(Turn.NON_TERMINAL - {Turn.QUEUED}))
            .first())
    if turn is None:
        return None
    agent = turn.acting_agent
    if agent is None or agent.user_id is None or agent.user_id != user.pk:
        return None
    session = turn.chat_session
    if session.workspace_id not in _tenant_slugs(user):
        return None
    return session
