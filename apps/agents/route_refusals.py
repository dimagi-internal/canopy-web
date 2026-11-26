"""Refusals the agent routers share: the HTTP answer for a zero-knowledge vault
error, and the rule that only an agent's owner or admins may turn `auto` on."""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.errors import TYPE_CONFLICT, TYPE_VALIDATION, ProblemError


def refuse_auto_unless_admin(request: HttpRequest, agent, what: str) -> None:
    """`auto` lets the agent act outbound without review, so anything that can turn
    it on — the agent's own switch, a turn-mode rule set to `auto`, a `manual` rule
    removed — is for the agent's owner and admins (docs/architecture/access.md).
    Anyone who may work the agent may make it more careful. `what` names the
    change, e.g. "setting a turn-mode rule api/anyone to auto"."""
    if not agent.is_admin(request.user):
        raise HttpError(
            403,
            f"{what} is for {agent.slug}'s owner or admins, because it can let the agent act "
            "without review. Setting manual is open to you; for auto, ask one of them, or ask "
            "the owner to make you an admin of the agent.",
        )


def zk_problem(exc) -> ProblemError:
    return ProblemError(exc.status, "Zero-knowledge vault", type_=(
        TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
        members={"error": exc.code})
