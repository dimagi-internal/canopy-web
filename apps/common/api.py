"""Django Ninja v2 router for the common surface (AI backend + /me/ + /health/)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.realtime.models import PresencePreference, show_presence_for

from .schemas import (
    HealthOut,
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

common_router = Router(auth=session_auth, tags=["common"])
public_router = Router(tags=["public"])


# --- /health/ (public) -----------------------------------------


@public_router.get("/health/", auth=None, response=HealthOut, summary="Health check")
def health(request: HttpRequest) -> HealthOut:
    return HealthOut(status="ok")


# --- /me/ (auth) -----------------------------------------------


@common_router.get("/me/", response=MeOut, summary="Current user")
def me(request: HttpRequest) -> MeOut:
    from apps.workspaces.services import can_create_workspace

    user = request.user
    avatar_url = ""
    social = (
        user.socialaccount_set.filter(provider="google").first()
        if hasattr(user, "socialaccount_set")
        else None
    )
    if social:
        avatar_url = social.extra_data.get("picture", "") or ""
    from apps.contacts import hcp
    from apps.contacts.models import Person

    memory_enabled = hcp.rollout_allows_address(user.email)
    memory_paused = (not memory_enabled
                     and hcp.has_memory(Person.objects.filter(user=user).first()))
    return MeOut(
        email=user.email,
        name=(user.get_full_name() or user.username or user.email),
        avatar_url=avatar_url,
        can_create_workspace=can_create_workspace(user),
        memory_enabled=memory_enabled,
        memory_paused=memory_paused,
    )


# --- /me/presence-preference/ (auth) ----------------------------


@common_router.get(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Get the current user's presence visibility preference",
)
def get_presence_preference(request: HttpRequest) -> PresencePreferenceOut:
    return PresencePreferenceOut(show_presence=show_presence_for(request.user))


@common_router.patch(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Set the current user's presence visibility preference",
)
def set_presence_preference(
    request: HttpRequest, payload: PresencePreferenceIn
) -> PresencePreferenceOut:
    PresencePreference.objects.update_or_create(
        user=request.user, defaults={"show_presence": payload.show_presence}
    )
    return PresencePreferenceOut(show_presence=payload.show_presence)


# --- /me/runner-app/ (auth) ------------------------------------


def _runner_app_out(opens_on: str) -> RunnerAppPreferenceOut:
    from apps.harness.models import RunnerAppPreference

    return RunnerAppPreferenceOut(
        opens_on=opens_on,
        choices=[RunnerAppChoice(value=v, label=label) for v, label in RunnerAppPreference.CHOICES],
    )


@common_router.get(
    "/me/runner-app/",
    response=RunnerAppPreferenceOut,
    summary="Get the page your Canopy Runner app opens on",
)
def get_runner_app_preference(request: HttpRequest) -> RunnerAppPreferenceOut:
    """The page the Canopy Runner desktop app's window opens on (it loads
    `/runner-app/`, which redirects there), and the pages it can open on —
    the app's menu-bar "Opens on" menu lists these. Unset: the supervisor."""
    from apps.harness.models import RunnerAppPreference

    return _runner_app_out(RunnerAppPreference.opens_on_for(request.user))


@common_router.patch(
    "/me/runner-app/",
    response=RunnerAppPreferenceOut,
    summary="Set the page your Canopy Runner app opens on",
)
def set_runner_app_preference(
    request: HttpRequest, payload: RunnerAppPreferenceIn
) -> RunnerAppPreferenceOut:
    """Takes effect the next time the app's window opens, on every box you use."""
    from apps.harness.models import RunnerAppPreference

    RunnerAppPreference.objects.update_or_create(
        user=request.user, defaults={"opens_on": payload.opens_on}
    )
    return _runner_app_out(payload.opens_on)


# --- /me/session-workspace/ (auth) ------------------------------


def _session_workspace_out(user) -> SessionWorkspacePreferenceOut:
    from apps.canopy_sessions import filing
    from apps.workspaces import permissions as perms
    from apps.workspaces.models import Workspace

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


@common_router.get(
    "/me/session-workspace/",
    response=SessionWorkspacePreferenceOut,
    summary="Get the workspace your own emdash sessions are filed in",
)
def get_session_workspace_preference(request: HttpRequest) -> SessionWorkspacePreferenceOut:
    """Your default for NEW sessions a runner you own discovers in emdash, when the
    emdash project is not an agent's (an agent's project follows the agent's
    workspace). Unset, or no longer one you can edit in: the runner's workspace."""
    return _session_workspace_out(request.user)


@common_router.patch(
    "/me/session-workspace/",
    response=SessionWorkspacePreferenceOut,
    summary="Set the workspace your own emdash sessions are filed in",
)
def set_session_workspace_preference(
    request: HttpRequest, payload: SessionWorkspacePreferenceIn
) -> SessionWorkspacePreferenceOut:
    """Set (or clear, with a null/blank `workspace`) your default. Must be a workspace
    you are an editor or better in. Applies to sessions discovered from now on;
    existing sessions stay where they are (move one with
    `POST /api/canopy-sessions/{id}/move`)."""
    from ninja.errors import HttpError

    from apps.canopy_sessions import filing

    try:
        filing.set_default(request.user, payload.workspace)
    except filing.FilingError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return _session_workspace_out(request.user)
