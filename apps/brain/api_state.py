"""`GET /api/brain/state`, `/state/links`, `/state/runs`, `/scorecard`: the brain as it stands, for the `/brain` page.

The person as themselves, or an admin agent's own login in its own turn pinned to a workspace it and the person
its turn answers to manage (`actor.of`, the management rule: never an OAuth, delegated, act-as or caller token, a
runner, or a learning run's own turn); each is an MCP tool. Read only, and only what the caller already reads: sessions
through legs 1 to 6, projects they may read, workspace-wide counts where they hold `logs.read`. WORKSPACE-SCOPED: the
page calls them pinned (`/api/w/<ws>/brain/…`), which covers `<ws>` and the workspaces below it the caller is in
(`wsvc.request_workspace_slugs`, the read rule every list follows). `apps/brain/state.py`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, ProblemError, refusals
from apps.api.route_gates import gate

from . import actor as actor_mod
from . import person, scope, scorecard, state
from .schemas import BrainLinkPageOut, BrainRunPageOut, BrainScorecardOut, BrainStateOut

router = Router(auth=session_auth, tags=["brain"])


def _viewer(request: HttpRequest):
    """Who reads the page: the person, or an admin agent pinned to a workspace it (and its person) manages."""
    actor = actor_mod.of(request)
    pin = getattr(request, "workspace_slug", None) or ""
    if actor.is_agent and not actor.may_manage(pin):
        raise ProblemError(403, "An agent reads the brain page only of a workspace it and its person manage, pinned "
                           "(/api/w/<ws>/…)", type_=TYPE_FORBIDDEN)
    return actor.user, pin


@router.get("/brain/state", response={200: BrainStateOut, **refusals(403, 422)},
            summary="Canopy learning as it stands, for you (counts; stores nothing)")
@gate("self")  # your own view: counts, and only rows of sessions and projects you already read
def brain_state(request: HttpRequest) -> dict:
    """Whether learners may read this workspace's sessions ("Learning reads") and its recent changes, how many people agreed to each class in your workspaces, the sources and
    why some are sealed, the stored links of sessions you read by project and tier, the learners and who answers for
    each (and whether you may configure it here), runs by state and the removal work still to do. Workspace-wide
    counts only where you hold `logs.read`; elsewhere your own. A consent count below 3 is null."""
    user, pin = _viewer(request)
    return state.summary(user, scope.of_request(request), pin)


@router.get("/brain/state/links", response={200: BrainLinkPageOut, **refusals(403, 422)},
            summary="Stored project links of sessions you read, newest first")
@gate("session-acl")  # only sessions you read (legs 1 to 6) and projects you may read
def brain_state_links(request: HttpRequest, before: int = 0, limit: int = 50) -> dict:
    """Each link with its session (title scrubbed) and how it was made. Pass `next_before` as `before` for more."""
    user, _pin = _viewer(request)
    return state.link_page(user, scope.of_request(request), before=before, limit=limit)


@router.get("/brain/state/runs", response={200: BrainRunPageOut, **refusals(403, 422)},
            summary="Learning runs about you or a project you read, newest first")
@gate("session-acl")  # a run's reads name only sessions you read; the rest are a count
def brain_state_runs(request: HttpRequest, before: int = 0, limit: int = 25) -> dict:
    """Each run's learner, purpose, subject and state, and what it read: the sessions you read by title, the rest a
    count. A canopy admin sees every run. Pass `next_before` as `before` for more."""
    user, _pin = _viewer(request)
    return state.run_page(user, scope.of_request(request), before=before, limit=limit)


@router.get("/brain/scorecard", response={200: BrainScorecardOut, **refusals(403, 422)},
            summary="How each learner target's findings land, over the last eight weeks (counts)")
@gate("self")  # counts over the findings you may read (outputs.visible_to), never wider
def brain_scorecard(request: HttpRequest) -> dict:
    """Per learner target here and below: the findings its runs made each week by where each stands now, the share
    taken up (precision: accepted and done over those decided), how many are open and how many faded, and how many came
    back after they were done. Only findings you may read are counted (the Findings tab's rule)."""
    actor = actor_mod.of(request)
    user, pin = _viewer(request)
    acting = actor if actor.is_agent else None
    return scorecard.scorecard(user, pin, scope.of_request(request), as_person=acting is None and person.is_person(
        request), acting=acting)
