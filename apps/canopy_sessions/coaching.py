"""Coaching uploads: which of a box's sessions its OWN runner ships in full.

A person who lets a coach (a live `SessionReviewGrant`) review their sessions has
also told their own machine to send those sessions up — canopy never reaches down
to a laptop for them, and a reviewer reads only stored rows. The runner already
ships every ACTIVE session it backs; this widens that, for a coached person, to
the sessions that stopped being active before their history was all sent
(`apps/harness/api_session_sync.list_streams`, the `ship_full` descriptor field).

Revoking the last grant empties the set on the runner's next poll; what was
already uploaded stays, deletable by the person as any session is.
"""
from __future__ import annotations

from django.db.models import Q

from apps.harness import runner_requirements as rr

from .access import sessions_of_q
from .models import Session, SessionReviewGrant


def is_coached(user_id) -> bool:
    """Whether this person has at least one live review grant."""
    if user_id is None:
        return False
    return SessionReviewGrant.objects.filter(user_id=user_id, revoked_at__isnull=True).exists()


def full_upload_session_ids(runner) -> set:
    """The ids of the sessions `runner` should ship in full for its owner's coach:
    the runner-discovered sessions it backs that are its owner's own work (not an
    agent's, not a contact's), never a sensitive one, and none at all from a
    sensitive-data box or a box whose owner has no live grant. The project opt-in
    is applied where every stream is (`_is_canopys`), not here."""
    if runner.owner_id is None or rr.is_sensitive_data_box(runner):
        return set()
    if not is_coached(runner.owner_id):
        return set()
    return set(
        Session.objects.filter(sessions_of_q([runner.owner_id]))
        .filter(Q(origin=Session.ORIGIN_RUNNER, created_by__isnull=True, agent__isnull=True,
                  contact__isnull=True, runner_binding__runner=runner))
        # Any runner requirement at all reads as sensitive (fails closed), the
        # same test the review-grant leg of the session ACL applies.
        .exclude(metadata__has_key=rr.METADATA_KEY)
        .values_list("id", flat=True)
    )
