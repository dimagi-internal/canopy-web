"""Coaching uploads: which of a box's sessions its OWN runner ships in full.

A person who opted into coaching (a live `CoachingOptIn`) has
also told their own machine to send those sessions up — canopy never reaches down
to a laptop for them, and a reviewer reads only stored rows. The runner already
ships every ACTIVE session it backs; this widens that, for a coached person, to
the sessions that stopped being active before their history was all sent
(`apps/harness/api_session_sync.list_streams`, the `ship_full` descriptor field).

Revoking the last grant empties the set on the runner's next poll (ACTIVE sessions
keep shipping, as they do for everyone). What was already uploaded stays until its
person deletes it: `DELETE /api/canopy-sessions/{id}/content`, the person's own act
(`content_deletion`), which works whether or not they are still coached and leaves a
tombstone so this set never lists that session again. `reset` re-ships a session's rows
and `archive` keeps them; neither deletes anything.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.harness import runner_requirements as rr

from . import lifecycle
from .access import sessions_of_q
from .models import CoachingOptIn, CoachingRole, Session


def is_coached(user_id) -> bool:
    """Whether this person has opted into coaching (a live `CoachingOptIn`)."""
    if user_id is None:
        return False
    return CoachingOptIn.objects.filter(user_id=user_id, revoked_at__isnull=True).exists()


def full_upload_session_ids(runner) -> set:
    """The ids of the sessions `runner` should ship in full for its owner's coach:
    the runner-discovered sessions it backs that are its owner's own work (not an
    agent's, not a contact's), never a sensitive one, never one whose words its person
    deleted, and none at all from a sensitive-data box or a box whose owner has no live
    grant. The project opt-in
    is applied where every stream is (`_is_canopys`), not here."""
    if runner.owner_id is None or rr.is_sensitive_data_box(runner):
        return set()
    if not is_coached(runner.owner_id):
        return set()
    return set(
        Session.objects.filter(sessions_of_q([runner.owner_id]))
        .filter(Q(origin=Session.ORIGIN_RUNNER, created_by__isnull=True, agent__isnull=True,
                  contact__isnull=True, content_deleted_at__isnull=True,
                  runner_binding__runner=runner))
        # Any runner requirement at all reads as sensitive (fails closed), the
        # same test the review-grant leg of the session ACL applies.
        .exclude(metadata__has_key=rr.METADATA_KEY)
        .values_list("id", flat=True)
    )


# ---- who coaches (a workspace owner's act) ----------------------------------------------------------

def designate(workspace, *, agent=None, user=None, by) -> CoachingRole:
    """Make an agent or a person a coach for `workspace` and everything below it. Doing it again is a
    no-op. The caller has checked `by` owns the workspace and that `agent`/`user` is one of its own."""
    assert (agent is None) != (user is None)
    with transaction.atomic():
        existing = CoachingRole.objects.filter(workspace=workspace, agent=agent, user=user,
                                               revoked_at__isnull=True).first()
        if existing is not None:
            return existing
        role = CoachingRole.objects.create(workspace=workspace, agent=agent, user=user, granted_by=by)
        lifecycle.record(lifecycle.SHARING_CHANGED, None, by=by, workspace_id=workspace.slug,
                         change={"field": "coach", "agent": agent.slug if agent else "",
                                 "user_id": user.pk if user else None, "before": False, "after": True})
    return role


def remove(role: CoachingRole, *, by) -> None:
    with transaction.atomic():
        role.revoked_at = timezone.now()
        role.save(update_fields=["revoked_at"])
        lifecycle.record(lifecycle.SHARING_CHANGED, None, by=by, workspace_id=role.workspace_id,
                         change={"field": "coach", "agent": role.agent.slug if role.agent_id else "",
                                 "user_id": role.user_id, "before": True, "after": False})


def roles_in(workspace) -> list[CoachingRole]:
    return list(CoachingRole.objects.filter(workspace=workspace, revoked_at__isnull=True)
                .select_related("agent", "user").order_by("created_at"))
