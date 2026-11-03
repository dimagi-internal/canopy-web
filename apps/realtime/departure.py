"""What leaving a workspace takes with it, for realtime: registered into `apps.workspaces.departure`."""
from __future__ import annotations

from django.db import transaction

from apps.workspaces import departure

from . import groups


def _close_sockets(user, left, by) -> None:
    """Ask every chat socket this person holds to re-check its access, so a
    session in a workspace they just left closes now rather than when they next
    act in it. Best-effort: a live socket also re-checks on every acting frame."""
    user_id, slugs = user.pk, list(left)

    def publish() -> None:
        groups.publish(groups.chat_user_group(user_id),
                       {"type": "access.recheck", "workspaces": slugs})

    transaction.on_commit(publish)


def register() -> None:
    departure.register("sockets", _close_sockets, order=90, counted=False)
