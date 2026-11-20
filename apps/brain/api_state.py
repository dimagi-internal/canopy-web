"""`GET /api/brain/state`, `/state/links`, `/state/runs`: the brain as it stands, for the `/brain` page.

PERSON-ONLY, refused as the preview refuses (`api_preview.the_person`: an agent's login, a turn's or caller token, a
runner, an OAuth client, a site's token, an MCP call), and an act-as-you token never reaches them (they are not in
`act_as.REST_READS`). Read only, and only what the person already reads: sessions through legs 1 to 6, projects they
may read, workspace-wide counts where they hold `logs.read`. `apps/brain/state.py`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc

from . import state
from .api_preview import the_person
from .schemas import BrainLinkPageOut, BrainRunPageOut, BrainStateOut

router = Router(auth=session_auth, tags=["brain"])


@router.get("/brain/state", response={200: BrainStateOut, **refusals(403)},
            summary="Canopy learning as it stands, for you (counts; stores nothing)")
@gate("self")  # your own view: counts, and only rows of sessions and projects you already read
def brain_state(request: HttpRequest) -> dict:
    """Whether learners may read at all, how many people agreed to each class in your workspaces, the sources and
    why some are sealed, the stored links of sessions you read by project and tier, the learners and who answers for
    each, runs by state and the removal work still to do. Workspace-wide counts only where you hold `logs.read`;
    elsewhere your own."""
    user = the_person(request)
    return state.summary(user, wsvc.request_member_slugs(request))


@router.get("/brain/state/links", response={200: BrainLinkPageOut, **refusals(403)},
            summary="Stored project links of sessions you read, newest first")
@gate("session-acl")  # only sessions you read (legs 1 to 6) and projects you may read
def brain_state_links(request: HttpRequest, before: int = 0, limit: int = 50) -> dict:
    """Each link with its session (title scrubbed) and how it was made. Pass `next_before` as `before` for more."""
    user = the_person(request)
    return state.link_page(user, wsvc.request_member_slugs(request), before=before, limit=limit)


@router.get("/brain/state/runs", response={200: BrainRunPageOut, **refusals(403)},
            summary="Learning runs about you or a project you read, newest first")
@gate("session-acl")  # a run's reads name only sessions you read; the rest are a count
def brain_state_runs(request: HttpRequest, before: int = 0, limit: int = 25) -> dict:
    """Each run's learner, purpose, subject and state, and what it read: the sessions you read by title, the rest a
    count. A canopy admin sees every run. Pass `next_before` as `before` for more."""
    user = the_person(request)
    return state.run_page(user, wsvc.request_member_slugs(request), before=before, limit=limit)
