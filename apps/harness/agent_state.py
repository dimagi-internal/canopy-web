"""An agent's state document: who may read and write it, and the versioned write.

It is whatever an agent would otherwise lose when it moves to another box, so it can
hold the agent's records about everyone it serves. Hence two different rules:

* WRITE: the agent's own login, inside a running, unconfined turn of that agent
  (`turn_asker.serving_turn`). A person's own full turn must be able to write, or their
  request to be forgotten would not reach the copy kept here.
* READ: the agent's owner or admins; or that same turn, only when it answers to nobody
  in particular or to one of them (`turn_asker.standing`, the rule of
  `access.review_grantors`), because a read returns the records of everyone. A turn a
  colleague started can write and cannot read.

A stored token of the agent's login is neither: it cannot show which turn it is in.
"""
from __future__ import annotations

import json

from django.db import IntegrityError, transaction

from . import turn_asker
from .models import AgentStateDocument

#: The most a document may be, as compact JSON.
MAX_BYTES = 1024 * 1024


class VersionConflict(Exception):
    """Someone wrote since the version this write names."""

    def __init__(self, current: int) -> None:
        super().__init__(f"current version is {current}")
        self.current = current


class TooLarge(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes")
        self.size = size


def writing_turn(user, agent):
    """The running turn of `agent` that `user`'s request is made inside, or None."""
    if not agent.is_own_login(user):
        return None
    turn = turn_asker.serving_turn(user)
    if turn is None:
        return None
    acting = turn_asker.turn_agent(turn)
    return turn if acting is not None and acting.pk == agent.pk else None


def may_read(user, agent) -> bool:
    if agent.is_own_login(user):
        if writing_turn(user, agent) is None:
            return False
        stand = turn_asker.standing(user)
        return stand is not None and (stand.unattended or stand.operator)
    return agent.is_admin(user)


def read(agent) -> AgentStateDocument | None:
    return AgentStateDocument.objects.filter(agent=agent).first()


def write(agent, *, expected_version: int, document: dict, turn) -> AgentStateDocument:
    """Replace the document if it is still at `expected_version`. Raises VersionConflict
    or TooLarge; nothing changes then."""
    size = len(json.dumps(document, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BYTES:
        raise TooLarge(size)
    with transaction.atomic():
        row = AgentStateDocument.objects.select_for_update().filter(agent=agent).first()
        current = row.version if row is not None else 0
        if current != expected_version:
            raise VersionConflict(current)
        if row is not None:
            row.document, row.version, row.updated_by_turn = document, current + 1, turn.pk
            row.save(update_fields=["document", "version", "updated_by_turn", "updated_at"])
            return row
        try:
            # Its own savepoint: two first writes race on the one-to-one, and a request's
            # rolling session still writes after this.
            with transaction.atomic():
                return AgentStateDocument.objects.create(agent=agent, document=document, version=1,
                                                         updated_by_turn=turn.pk)
        except IntegrityError:
            raise VersionConflict(read(agent).version) from None
