"""Routes a PERSON must call from the canopy web app, never a token.

A token acts with its user's whole role, and since 2026-09-29 every REST route
is also an MCP tool — so anything an owner can do, a script, an agent session
holding the owner's PAT, or an MCP client signed in for an hour can do too. For
most routes that is the point. For a handful it is the risk itself: minting
administrators, publishing what outsiders may make an agent do, writing an
agent's credentials, removing people, minting a never-expiring token from a
one-hour one. Those are decisions somebody should be looking at a screen to
make, and an agent on its owner's laptop must not be able to make them for it.

    @router.put("/{slug}/vault", ...)
    @human_only("an agent's vault")
    def set_agent_vault(request, ...): ...

The decorator goes UNDER the route decorator. It does not wrap the function
Ninja reads (a wrapper would lose the view's module globals, which Ninja needs
to resolve its string annotations); it hooks the built `Operation` and wraps
its `view_func` after the signature is taken, so the refusal runs after
authentication (an anonymous caller still gets 401) and before the body, as an
ordinary `HttpError` rendered as problem+json.

The refusal comes before any lookup, so it is the same 403 whether or not the
resource exists — it says nothing about what is there.

Every decorated view is recorded in `HUMAN_ONLY`, and
`tests/test_human_only_routes.py` fails if one of them is offered as an MCP
tool: a tool that always refuses is noise in every client's tool list, and the
exclusion is where the reason is written down.
"""
from __future__ import annotations

import functools
from typing import Callable

from ninja.errors import HttpError
from ninja.utils import contribute_operation_callback

from .views_debug import is_machine

#: `(module, function name)` of every human-only view → what it changes.
HUMAN_ONLY: dict[tuple[str, str], str] = {}

#: The reason a human-only route is not an MCP tool (`apps/mcp/api_tools.py`).
MCP_REASON = "refused to any token by design (`human_only`): canopy's web app only"


def refuse_machine(request, what: str) -> None:
    """403 when a token, not a person in a browser, is behind `request`."""
    if is_machine(request):
        raise HttpError(
            403,
            f"{what} can only be changed by a person, from the canopy web app — "
            "not with a token or an MCP client. Open canopy and do it there.",
        )


def human_only(what: str) -> Callable:
    """Refuse machine callers on this route. `what` names the thing it changes,
    for the refusal message ("an agent's credentials")."""

    def decorate(view: Callable) -> Callable:
        HUMAN_ONLY[(view.__module__, view.__name__)] = what

        def install(operation) -> None:
            inner = operation.view_func

            @functools.wraps(inner)
            def guarded(request, *args, **kwargs):
                refuse_machine(request, what)
                return inner(request, *args, **kwargs)

            operation.view_func = guarded

        contribute_operation_callback(view, install)
        view.human_only = what
        return view

    return decorate
