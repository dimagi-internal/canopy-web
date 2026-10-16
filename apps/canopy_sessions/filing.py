"""Which workspace a session is FILED in — the default for a new one, and moving one.

The owner's ask (Jonathan, 2026-10-11): "I still want all my sessions that I do in
emdash to be associated with a default I can set, which is Dimagi, and then if I
belong to more than one workspace, I could move the session's association if I
want."

Three rules, one module:

* **Filing a NEW runner-discovered session** (`filing_workspace`). A project that
  names an agent keeps following that agent's workspace — that is the agent's
  tenant, not the person's, and it is decided in `harness.services` before this is
  asked. For every other session the runner OWNER's default session workspace wins
  while they can still write there; otherwise `runner.workspace`, which is what
  every session got before this existed. Existing sessions are never re-filed: the
  report loop only asks this when it creates a row, so a session somebody moved
  stays where they put it however many times the runner re-reports it.

* **The default itself** (`set_default`). A workspace the person can write content
  in (`content.write`, i.e. editor or better) — filing a session somewhere puts a
  row in that tenant, the same write `move` gates on. Re-checked at filing time, so
  leaving the workspace (or being demoted to viewer) silently falls back.

* **Moving a session** (`move`). Who: the person whose session it is — its
  creator for a web chat, the owner of the runner it was discovered on for a
  runner session. Where: a workspace they can write content in. Never an agent's
  session: that lives in the agent's tenant, and moving it would hide the agent's
  own work from the people who run it (422).

  Moving is TENANT-FIRST (`access.py`): after the move, only members of the
  target can see the session. Explicit participants who are not members of the
  target lose their participant row — the same sweep `workspaces/departure.py`
  does when someone leaves a workspace, and for the same reason: a dormant grant
  would otherwise wake up the day they joined the target. Their open chat sockets
  are asked to re-check access and close. Moving it back does not restore those
  rows; share it again.

  Nothing else needs to follow, because nothing else under a session carries its
  own tenant: messages, attachments, drafts, secrets, keys, page actions, transfer
  requests and the runner binding hang off the session, and a session turn DERIVES
  its tenant from `chat_session.workspace` (`Turn.workspace` is set only on a
  project turn, and `turn_targets_agent_xor_project_xor_session` forbids a turn
  from being both a project turn and a session turn). Rows in OTHER products that
  merely point at a session (a storyboard, a review, a walkthrough) keep their own
  workspace — they are that tenant's content, and the link does not grant access
  to the session.
"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .models import Session, SessionParticipant, SessionWorkspacePreference

logger = logging.getLogger(__name__)


class FilingError(ValueError):
    """A refused default or move. `status` is the HTTP status the API answers."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _may_file_in(user, workspace) -> bool:
    return perms.can(user, workspace, perms.CONTENT_WRITE)


# ---- the default -----------------------------------------------------------


def stored_default(user) -> Workspace | None:
    """The workspace the person chose, as stored — whether or not it still applies."""
    if user is None or not getattr(user, "pk", None):
        return None
    pref = (SessionWorkspacePreference.objects.select_related("workspace")
            .filter(user=user).first())
    return pref.workspace if pref is not None else None


def effective_default(user) -> Workspace | None:
    """The person's default, if it still APPLIES: set, and they can still write
    there. `None` means "use the runner's workspace"."""
    ws = stored_default(user)
    if ws is None or not _may_file_in(user, ws):
        return None
    return ws


def set_default(user, slug: str | None) -> Workspace | None:
    """Set (or with a blank slug, clear) the person's default session workspace."""
    slug = (slug or "").strip()
    if not slug:
        SessionWorkspacePreference.objects.filter(user=user).delete()
        return None
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or not wsvc.is_member(user, ws.slug):
        # Same answer for "no such workspace" and "not yours": no existence leak.
        raise FilingError(f"workspace '{slug}' not found or you are not a member", status=404)
    if not _may_file_in(user, ws):
        raise FilingError(
            f"you are a viewer in '{slug}'; sessions can only be filed where you can edit")
    SessionWorkspacePreference.objects.update_or_create(user=user, defaults={"workspace": ws})
    return ws


def filing_workspace(runner, fallback):
    """Where a NEW agent-less session reported by `runner` is filed: its owner's
    default while it still applies, else `fallback` (the runner's workspace)."""
    owner = getattr(runner, "owner", None)
    if owner is None:
        return fallback
    try:
        return effective_default(owner) or fallback
    except Exception:  # noqa: BLE001 — a preference must never cost a liveness report
        logger.warning("could not read the session workspace default of runner %s's owner",
                       getattr(runner, "pk", None), exc_info=True)
        return fallback


# ---- moving ----------------------------------------------------------------


def _runner_owned_by(session: Session, user) -> bool:
    """Is `user` the owner of the runner this session was discovered on?

    The binding's live `runner` FK first. When the session has fallen off the
    report that FK is cleared, so recover the box from the binding's `host` — the
    same identity the report loop itself recovers the row by (never a blank host,
    which any un-heartbeated runner shares)."""
    from apps.harness.models import Runner

    binding = getattr(session, "runner_binding", None)
    if binding is None:
        return False
    if binding.runner_id is not None:
        return binding.runner.owner_id == user.pk
    if not binding.host:
        return False
    return Runner.objects.filter(host=binding.host, owner=user).exists()


def move_refusal(user, session: Session) -> str | None:
    """Why `user` may not move `session` anywhere, or None if they may.

    Independent of the target, so the UI can ask it once per session."""
    if session.agent_id is not None:
        return ("this is an agent's session; it belongs to the agent's workspace "
                f"('{session.agent.workspace_id}') and moves only with the agent "
                f"(POST /api/agents/{session.agent.slug}/move)")
    if session.created_by_id is not None:
        if session.created_by_id == user.pk:
            return None
        return "only the person who started this chat can move it"
    if session.origin == Session.ORIGIN_RUNNER and _runner_owned_by(session, user):
        return None
    return "only the owner of the runner this session was discovered on can move it"


def can_move(user, session: Session) -> bool:
    return move_refusal(user, session) is None


def move(session: Session, target_slug: str, *, by) -> dict:
    """Re-file `session` in `target_slug`. Returns what changed.

    The caller has already resolved `session` through the read gate (a session
    you cannot see is a 404 before this runs)."""
    refusal = move_refusal(by, session)
    if refusal is not None:
        # An agent's session is a property of the session (422); anything else is
        # a property of the caller (403).
        raise FilingError(refusal, status=422 if session.agent_id is not None else 403)
    target = Workspace.objects.filter(slug=(target_slug or "").strip()).first()
    if target is None or not wsvc.is_member(by, target.slug):
        raise FilingError(f"workspace '{target_slug}' not found or you are not a member",
                          status=404)
    if not _may_file_in(by, target):
        raise FilingError(f"you are a viewer in '{target.slug}'; you can only move a "
                          "session to a workspace where you can edit", status=403)
    source = session.workspace_id
    if source == target.slug:
        return {"moved": False, "from": source, "to": target.slug, "participants_removed": 0}

    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        locked.workspace = target
        locked.save(update_fields=["workspace", "updated_at"])
        # Tenant-first: a participant outside the target can no longer read it, so
        # their row goes too (see the module docstring for why it is not kept).
        target_members = wsvc.member_user_ids(target)
        dropped = list(
            SessionParticipant.objects.filter(session=locked)
            .exclude(user_id__in=target_members).values_list("user_id", flat=True)
        )
        if dropped:
            SessionParticipant.objects.filter(session=locked, user_id__in=dropped).delete()
        # Everyone who could have it open: the old tenant's members not in the new one.
        lost = (wsvc.member_user_ids(source) | set(dropped)) - target_members
        transaction.on_commit(lambda: _recheck_sockets(lost, source))

    session.workspace = target
    session.workspace_id = target.slug
    logger.info("session %s moved %s -> %s by user %s", session.pk, source, target.slug, by.pk)
    return {"moved": True, "from": source, "to": target.slug,
            "participants_removed": len(dropped)}


def rehome_agent_sessions(sessions, target: Workspace, *, apply: bool) -> tuple[int, int]:
    """Re-file an AGENT's sessions in `target`, as part of moving the agent
    (`apps/agents/move.py`) — the one way an agent's session moves, since
    `move_refusal` keeps it with its agent. Returns (sessions, participants
    dropped); with `apply=False` it only counts.

    Same tenant-first rule as `move`: a participant who is not a member of the
    target can no longer read the chat, so their row goes. The caller owns the
    transaction."""
    sessions = sessions.exclude(workspace_id=target.slug)
    target_members = wsvc.member_user_ids(target)
    participants = SessionParticipant.objects.filter(session__in=sessions).exclude(
        user_id__in=target_members)
    if not apply:
        return sessions.count(), participants.count()
    sources = set(sessions.values_list("workspace_id", flat=True))
    dropped_users = set(participants.values_list("user_id", flat=True))
    dropped = participants.delete()[0]
    moved = sessions.update(workspace=target)
    for source in sources:
        lost = (wsvc.member_user_ids(source) | dropped_users) - target_members
        transaction.on_commit(lambda lost=lost, source=source: _recheck_sockets(lost, source))
    return moved, dropped


def _recheck_sockets(user_ids, old_slug: str) -> None:
    """Ask each open chat socket these people hold on a session in `old_slug` to
    re-check its access — the same frame `workspaces/departure.py` publishes. The
    socket re-asks `access.role_for`, which now sees the new workspace."""
    from apps.realtime import groups

    for uid in user_ids:
        try:
            groups.publish(groups.chat_user_group(uid),
                           {"type": "access.recheck", "workspaces": [old_slug]})
        except Exception:  # noqa: BLE001 — best-effort, the socket re-checks on every act
            logger.warning("could not ask user %s's chat sockets to re-check", uid, exc_info=True)
