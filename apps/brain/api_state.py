"""`GET /api/brain/state`, `/state/links`, `/state/runs`: the brain as it stands, for the `/brain` page.

PERSON-ONLY (`the_person`: never an agent's login, a turn's or caller token, an act-as or OAuth token, a runner, a
site's token or an MCP call), and none is an MCP tool. Read only, and only what the person already reads: sessions
through legs 1 to 6, projects they may read, workspace-wide counts where they hold `logs.read`. WORKSPACE-SCOPED: the
page calls them pinned (`/api/w/<ws>/brain/…`), which covers `<ws>` and the workspaces below it the caller is in
(`wsvc.request_workspace_slugs`, the read rule every list follows). `apps/brain/state.py`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, ProblemError, refusals
from apps.api.route_gates import gate
from apps.contacts import people
from apps.harness import acting_person
from apps.tokens import kinds
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import state
from .schemas import BrainLinkPageOut, BrainRunPageOut, BrainStateOut

router = Router(auth=session_auth, tags=["brain"])

#: The credentials a person uses as themselves: their browser session or a PAT they minted by hand.
PERSON_KINDS = frozenset({kinds.SESSION, kinds.PAT})


def the_person(request: HttpRequest):
    """The person, as themselves, else a 403: never an agent's login, a turn's or caller token, an act-as or OAuth
    token, a runner, a site's token or an MCP call."""
    kind = kinds.of_request(request)
    user = getattr(request, "user", None)
    if (getattr(request, "via_mcp", False) or kind is None or kind.name not in PERSON_KINDS
            or not people.is_human_account(user) or acting_person.refusal(request) is not None):
        raise ProblemError(403, "Only the person, as themselves, may see this", type_=TYPE_FORBIDDEN)
    return user


@router.get("/brain/state", response={200: BrainStateOut, **refusals(403, 422)},
            summary="Canopy learning as it stands, for you (counts; stores nothing)")
@gate("self")  # your own view: counts, and only rows of sessions and projects you already read
def brain_state(request: HttpRequest) -> dict:
    """Whether learners may read at all, how many people agreed to each class in your workspaces, the sources and
    why some are sealed, the stored links of sessions you read by project and tier, the learners and who answers for
    each, runs by state and the removal work still to do. Workspace-wide counts only where you hold `logs.read`;
    elsewhere your own."""
    user = the_person(request)
    pin = getattr(request, "workspace_slug", None)
    return {**state.summary(user, wsvc.request_workspace_slugs(request)), "workspace": pin or "",
            "can_run": bool(pin) and (bool(getattr(user, "is_superuser", False)) or perms.can(user, pin, perms.OWN))}


@router.get("/brain/state/links", response={200: BrainLinkPageOut, **refusals(403, 422)},
            summary="Stored project links of sessions you read, newest first")
@gate("session-acl")  # only sessions you read (legs 1 to 6) and projects you may read
def brain_state_links(request: HttpRequest, before: int = 0, limit: int = 50) -> dict:
    """Each link with its session (title scrubbed) and how it was made. Pass `next_before` as `before` for more."""
    user = the_person(request)
    return state.link_page(user, wsvc.request_workspace_slugs(request), before=before, limit=limit)


@router.get("/brain/state/runs", response={200: BrainRunPageOut, **refusals(403, 422)},
            summary="Learning runs about you or a project you read, newest first")
@gate("session-acl")  # a run's reads name only sessions you read; the rest are a count
def brain_state_runs(request: HttpRequest, before: int = 0, limit: int = 25) -> dict:
    """Each run's learner, purpose, subject and state, and what it read: the sessions you read by title, the rest a
    count. A canopy admin sees every run. Pass `next_before` as `before` for more."""
    user = the_person(request)
    return state.run_page(user, wsvc.request_workspace_slugs(request), before=before, limit=limit)
