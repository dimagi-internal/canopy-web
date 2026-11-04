"""What leaving a workspace takes with it, for canopy_sessions: registered into `apps.workspaces.departure`."""
from __future__ import annotations

from apps.workspaces import departure

from .models import SessionParticipant


def _session_participants(user, left, by) -> int:
    return SessionParticipant.objects.filter(user=user, session__workspace_id__in=left).delete()[0]


def register() -> None:
    departure.register("session_participants", _session_participants, order=30)
