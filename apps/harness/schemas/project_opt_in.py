"""Project opt-in schemas."""
from __future__ import annotations

from ninja import Schema
from pydantic import Field


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


class ProjectOptInIn(Schema):
    """Request body for PUT /api/me/canopy-projects/."""

    everything: bool = False
    projects: list[str] = []
