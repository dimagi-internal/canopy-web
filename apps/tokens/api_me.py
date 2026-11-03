"""The caller's own page: who they are (`/me/`) and the preferences that follow them
across workspaces — presence, the page the Canopy Runner app opens on, where their own
sessions are filed. Served under `/api/me/…`; it reads the contacts, realtime, harness and
sessions apps, so it sits with the tokens (the person's own credentials), above `common`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.canopy_sessions import filing
from apps.common.schemas import (
    MeOut,
    PresencePreferenceIn,
    PresencePreferenceOut,
    RunnerAppChoice,
    RunnerAppPreferenceIn,
    RunnerAppPreferenceOut,
    SessionWorkspaceChoice,
    SessionWorkspacePreferenceIn,
    SessionWorkspacePreferenceOut,
)
from apps.common.views_debug import is_machine
from apps.contacts import hcp
from apps.contacts.models import Person
from apps.harness.models import RunnerAppPreference
from apps.realtime.models import PresencePreference, show_presence_for
from apps.workspaces import permissions as perms
from apps.workspaces.login_gate import can_create_workspace
from apps.workspaces.models import Workspace

router = Router(auth=session_auth, tags=["common"])


# --- /me/ (auth) -----------------------------------------------


@router.get("/me/", response=MeOut, summary="Current user")
@gate("authenticated")
def me(request: HttpRequest) -> MeOut:
    user = request.user
    avatar_url = ""
    social = (
        user.socialaccount_set.filter(provider="google").first()
        if hasattr(user, "socialaccount_set")
        else None
    )
    if social:
        avatar_url = social.extra_data.get("picture", "") or ""
    memory_enabled = hcp.rollout_allows_address(user.email)
    memory_paused = (not memory_enabled
                     and hcp.has_memory(Person.objects.filter(user=user).first()))
    return MeOut(
        user_id=user.id,
        email=user.email,
        name=(user.get_full_name() or user.username or user.email),
        avatar_url=avatar_url,
        can_create_workspace=can_create_workspace(user),
        memory_enabled=memory_enabled,
        memory_paused=memory_paused,
        passkeys_here=not is_machine(request),
    )


# --- /me/presence-preference/ (auth) ----------------------------


@router.get(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Get the current user's presence visibility preference",
)
@gate("self")
def get_presence_preference(request: HttpRequest) -> PresencePreferenceOut:
    return PresencePreferenceOut(show_presence=show_presence_for(request.user))


@router.patch(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Set the current user's presence visibility preference",
)
@gate("self", setting=True)
def set_presence_preference(
    request: HttpRequest, payload: PresencePreferenceIn
) -> PresencePreferenceOut:
    PresencePreference.objects.update_or_create(
        user=request.user, defaults={"show_presence": payload.show_presence}
    )
    return PresencePreferenceOut(show_presence=payload.show_presence)


# --- /me/runner-app/ (auth) ------------------------------------


def _runner_app_out(opens_on: str) -> RunnerAppPreferenceOut:
    return RunnerAppPreferenceOut(
        opens_on=opens_on,
        choices=[RunnerAppChoice(value=v, label=label) for v, label in RunnerAppPreference.CHOICES],
    )


@router.get(
    "/me/runner-app/",
    response=RunnerAppPreferenceOut,
    summary="Get the page your Canopy Runner app opens on",
)
@gate("self")
def get_runner_app_preference(request: HttpRequest) -> RunnerAppPreferenceOut:
    """The page the Canopy Runner desktop app's window opens on (it loads
    `/runner-app/`, which redirects there), and the pages it can open on —
    the app's menu-bar "Opens on" menu lists these. Unset: the supervisor."""
    return _runner_app_out(RunnerAppPreference.opens_on_for(request.user))


@router.patch(
    "/me/runner-app/",
    response=RunnerAppPreferenceOut,
    summary="Set the page your Canopy Runner app opens on",
)
@gate("self", setting=True)
def set_runner_app_preference(
    request: HttpRequest, payload: RunnerAppPreferenceIn
) -> RunnerAppPreferenceOut:
    """Takes effect the next time the app's window opens, on every box you use."""
    RunnerAppPreference.objects.update_or_create(
        user=request.user, defaults={"opens_on": payload.opens_on}
    )
    return _runner_app_out(payload.opens_on)


# --- /me/session-workspace/ (auth) ------------------------------


def _session_workspace_out(user) -> SessionWorkspacePreferenceOut:
    stored = filing.stored_default(user)
    effective = filing.effective_default(user)
    slugs = perms.slugs_with(user, perms.CONTENT_WRITE)
    choices = [
        SessionWorkspaceChoice(slug=ws.slug, display_name=ws.display_name or ws.slug)
        for ws in Workspace.objects.filter(slug__in=slugs).order_by("slug")
    ]
    return SessionWorkspacePreferenceOut(
        workspace=stored.slug if stored else None,
        effective=effective.slug if effective else None,
        choices=choices,
    )


@router.get(
    "/me/session-workspace/",
    response=SessionWorkspacePreferenceOut,
    summary="Get the workspace your own emdash sessions are filed in",
)
@gate("self")
def get_session_workspace_preference(request: HttpRequest) -> SessionWorkspacePreferenceOut:
    """Your default for NEW sessions a runner you own discovers in emdash, when the
    emdash project is not an agent's (an agent's project follows the agent's
    workspace). Unset, or no longer one you can edit in: the runner's workspace."""
    return _session_workspace_out(request.user)


@router.patch(
    "/me/session-workspace/",
    response=SessionWorkspacePreferenceOut,
    summary="Set the workspace your own emdash sessions are filed in",
)
@gate("self", setting=True)  # target needs content.write (filing.set_default)
def set_session_workspace_preference(
    request: HttpRequest, payload: SessionWorkspacePreferenceIn
) -> SessionWorkspacePreferenceOut:
    """Set (or clear, with a null/blank `workspace`) your default. Must be a workspace
    you are an editor or better in. Applies to sessions discovered from now on;
    existing sessions stay where they are (move one with
    `POST /api/canopy-sessions/{id}/move`)."""
    try:
        filing.set_default(request.user, payload.workspace)
    except filing.FilingError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return _session_workspace_out(request.user)
