"""/api/harness runner routes: admins, open sessions, and enrolled workspaces.
"""
from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.http import HttpRequest
from ninja import Status
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.canopy_sessions import control
from apps.workspaces import services as wsvc

from . import (
    routing,
    runner_sessions,
)
from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_admin_or_404,
    runner_or_404,
)
from .models import (
    Runner,
)
from .schemas import (
    RunnerAdminIn,
    RunnerAdminOut,
    RunnerEnrollmentIn,
    RunnerEnrollmentOut,
    RunnerOpenSessionCloseOut,
    RunnerOpenSessionOut,
)


def _admin_row(a) -> dict:
    return {"user_id": a.user_id, "email": a.user.email,
            "granted_by_email": a.granted_by.email if a.granted_by else "",
            "created_at": a.created_at}


@router.get("/runners/{runner_id}/admins", response=list[RunnerAdminOut],
            summary="Who may administer this runner")
@gate("runner-admin")
def list_runner_admins(request: HttpRequest, runner_id: uuid.UUID):
    """Visible to anyone who can already administer the box — the answer to
    "who else can fix this", which is the question a stuck box raises."""
    runner = runner_admin_or_404(request, runner_id)
    return [_admin_row(a) for a in harness_runners.list_runner_admins(runner)]


@router.get("/runners/{runner_id}/sessions/", response=list[RunnerOpenSessionOut],
            summary="The sessions open on this runner, facts only (owner and admins)")
@gate("runner-admin")
def list_runner_sessions(request: HttpRequest, runner_id: uuid.UUID):
    """Every session counted in this runner's `open_sessions`, whoever's it is: who
    started it and how, its turns, when it last did anything, and `open_reason` —
    the check that holds it open (`staleness.OPEN_REASONS`). Never a title, prompt
    or message: the words stay with the people in the conversation, and this is what
    the people who keep the box need to understand and tidy it. Longest-quiet first,
    at most 500. See `apps/harness/runner_sessions.py`."""
    runner = runner_admin_or_404(request, runner_id)
    return runner_sessions.open_sessions(runner)


@router.post("/runners/{runner_id}/sessions/{session_id}/close", response=RunnerOpenSessionCloseOut,
             summary="Close an idle session on this runner (owner and admins)")
@gate("runner-admin", kind="conversation")
def close_runner_session(request: HttpRequest, runner_id: uuid.UUID, session_id: uuid.UUID):
    """Close one session open on this box, as housekeeping: the same close as the
    session's own `/close`, for the people trusted with emptying the box (who may
    already move it off, `access.may_move`). Refuses one in use — a turn running, a
    question waiting, or mid-transfer (`reason: "in_use:<open reason>"`) — since that
    would end somebody's live conversation. `not_open` when it is not open here.
    A cloud session's close is an archive, which `/unarchive` undoes."""
    runner = runner_admin_or_404(request, runner_id)
    session, reason = runner_sessions.open_session(runner, session_id)
    if session is None:
        return {"ok": False, "reason": "not_open"}
    if reason in runner_sessions.IN_USE:
        return {"ok": False, "reason": f"in_use:{reason}"}
    outcome = control.close_session(session=session)
    ok = outcome in ("closing", "closed")
    return {"ok": ok, "closing": outcome == "closing", "reason": "" if ok else outcome}


@router.post("/runners/{runner_id}/admins", response=RunnerAdminOut,
             summary="Grant someone administration of this runner (owner only)")
@gate("runner", setting=True)  # owner only (not RunnerAdmins)
def grant_runner_admin(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerAdminIn):
    """Granting stays with the OWNER, not with grantees.

    Deliberate: an administrator can change what the box runs on, but letting
    them mint more administrators makes the grant self-propagating, and then the
    explicit list stops being a list of people the owner actually trusted.
    """
    runner = runner_or_404(request, runner_id)
    user = User.objects.filter(email__iexact=payload.email.strip()).first()
    if user is None:
        # No leak either way: the caller already owns this runner, and "no such
        # account" is the only useful thing to say about a typo'd address.
        raise HttpError(404, f"no account with email {payload.email!r}")
    # Same tenant leg the admin resolver enforces, checked here so the failure
    # is a clear 422 at grant time rather than a mystifying 404 the first time
    # the grantee tries to use it.
    if runner.workspace_id and not wsvc.is_member(user, runner.workspace_id):
        raise HttpError(
            422,
            f"{user.email} is not a member of the workspace this runner belongs to",
        )
    return _admin_row(harness_runners.grant_runner_admin(runner, user, granted_by=request.user))


@router.delete("/runners/{runner_id}/admins/{user_id}", response={204: None},
               summary="Revoke administration (owner only)")
@gate("runner", setting=True)
def revoke_runner_admin(request: HttpRequest, runner_id: uuid.UUID, user_id: int):
    runner = runner_or_404(request, runner_id)
    user = User.objects.filter(pk=user_id).first()
    if user is None or not harness_runners.revoke_runner_admin(runner, user):
        raise HttpError(404, "no such grant on this runner")
    return Status(204, None)


def _enrollment_row(request: HttpRequest, runner: Runner, e, serving: set[str]) -> dict:
    below = wsvc.descendant_slugs({e.workspace_id}) & serving
    return {"workspace": e.workspace_id, "display_name": e.workspace.display_name,
            "serving": e.workspace_id in serving,
            "covers": sorted(s for s in below if _sees_enrollment(request, runner, s)),
            "enrolled_by_email": e.enrolled_by.email if e.enrolled_by else "",
            "created_at": e.created_at}


def _sees_enrollment(request: HttpRequest, runner: Runner, slug: str) -> bool:
    """The owner sees every workspace their box serves; an administrator sees
    only those they are a member of themselves — administering a box is not a
    reason to learn which other tenants its owner belongs to."""
    return runner.owner_id == request.user.id or wsvc.is_member(request.user, slug)


@router.get("/runners/{runner_id}/workspaces", response=list[RunnerEnrollmentOut],
            summary="The workspaces this runner serves (its enrollments)")
@gate("runner-admin")  # an admin sees only workspaces it is a member of
def list_runner_workspaces(request: HttpRequest, runner_id: uuid.UUID):
    """A runner serves a workspace only when it is ENROLLED there and its owner
    may run work there (editor and above) — `serving` says whether both hold.
    An enrollment covers the workspaces below it too, including ones created
    later (`covers` lists those it serves). Pairing enrolls a box only in the workspace it paired into; joining another
    workspace does not enroll your existing boxes."""
    runner = runner_admin_or_404(request, runner_id)
    serving = routing.runner_tenant_slugs(runner)
    return [_enrollment_row(request, runner, e, serving) for e in harness_runners.list_runner_enrollments(runner)
            if _sees_enrollment(request, runner, e.workspace_id)]


@router.post("/runners/{runner_id}/workspaces", response=RunnerEnrollmentOut,
             summary="Enroll this runner in a workspace so it serves its work")
@gate("runner-admin", setting=True)  # + the runner's OWNER holds agent.work there (422)
def enroll_runner_workspace(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerEnrollmentIn):
    """Opt the box in to a workspace's turns — its agents' (as routing assigns
    them), its repo turns and its unbound chats — and those of every workspace
    below it, now or later, wherever its owner is an editor. Idempotent.

    The owner or a runner administrator may ask; the box's OWNER must be an
    editor or above there (422 otherwise), because a box works with its owner's
    memberships and enrollment never widens them. A caller who is not a member
    of the workspace gets the same 404 as a workspace that does not exist.
    """
    runner = runner_admin_or_404(request, runner_id)
    slug = (payload.workspace or "").strip()
    # The CALLER must be a member to name it — owner or administrator alike —
    # so a missing workspace and someone else's read the same.
    if not slug or not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    try:
        row = harness_runners.enroll_runner(runner, slug, by=request.user)
    except harness_runners.EnrollmentRefused as exc:
        raise HttpError(422, str(exc))
    return _enrollment_row(request, runner, row, routing.runner_tenant_slugs(runner))


@router.delete("/runners/{runner_id}/workspaces/{workspace}", response={204: None},
               summary="Stop this runner serving a workspace")
@gate("runner-admin", setting=True)
def unenroll_runner_workspace(request: HttpRequest, runner_id: uuid.UUID, workspace: str):
    """The box claims nothing more there from its next poll — unless a
    workspace above it is still enrolled, which covers it; a turn it already
    holds runs to completion. Owner or a runner administrator."""
    runner = runner_admin_or_404(request, runner_id)
    if not _sees_enrollment(request, runner, workspace) or not harness_runners.unenroll_runner(
            runner, workspace):
        raise HttpError(404, "this runner is not enrolled in that workspace")
    return Status(204, None)
