"""Project opt-in schemas."""
from __future__ import annotations

from ninja import Schema
from pydantic import Field


class ProjectOptInWorkspaceOut(Schema):
    slug: str
    display_name: str


class ProjectOptInOut(Schema):
    """Response for GET/PUT /api/me/canopy-projects/."""

    everything: bool = Field(description="Canopy works in every repo on your boxes.")
    projects: list[str] = Field(
        description="The repos canopy works in when `everything` is false. Every other "
        "repo is plain Claude Code: nothing from it is routed, mirrored or read."
    )
    available: list[str] = Field(
        description="Repos your own runners report holding, plus any you named — what to choose from."
    )
    repo_workspaces: dict[str, list[str]] = Field(
        description="The workspaces you assigned each repo to. A repo chat or repo turn runs "
        "in those workspaces and the ones below them, and nowhere else."
    )
    default_workspaces: dict[str, list[str]] = Field(
        description="Where a repo you have not assigned belongs: the home workspace of each "
        "of your runners holding it."
    )
    workspaces: list[ProjectOptInWorkspaceOut] = Field(
        description="The workspaces you may assign a repo to: those where you may run work."
    )


class ProjectOptInIn(Schema):
    """Request body for PUT /api/me/canopy-projects/."""

    everything: bool = False
    projects: list[str] = []
    # Replaces every assignment when present; absent leaves them as they are.
    repo_workspaces: dict[str, list[str]] | None = None
