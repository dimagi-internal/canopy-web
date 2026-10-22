"""Refusals the agent routers share: the HTTP answer for a zero-knowledge vault
error, and the rule that only an agent's owner or admins may set `auto`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.errors import TYPE_CONFLICT, TYPE_VALIDATION, ProblemError


def refuse_auto_unless_admin(request: HttpRequest, agent, what: str) -> None:
    """`auto` lets the agent act outbound without review, so SETTING it — on a
    routing rule, an actor route or the agent's own switch — is for the agent's
    owner and admins (docs/architecture/access.md). A workspace editor may still
    route the agent's work and set `manual`; lowering autonomy is safe from
    anyone. Before 2026-10-04 an editor could write `turn_mode: auto` on a rule,
    which ran every matching turn auto — around the dispatch gate (#1102) that
    keeps `auto` from them."""
    if not agent.is_admin(request.user):
        raise HttpError(
            403,
            f"{what} with turn_mode=auto is for {agent.slug}'s owner or admins; you may set "
            "it with turn_mode manual or '' (defer), or ask its owner to make you an admin",
        )


def zk_problem(exc) -> ProblemError:
    return ProblemError(exc.status, "Zero-knowledge vault", type_=(
        TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
        members={"error": exc.code})
