"""Pydantic schemas for the /api/projects + /api/insights surface."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

ProjectVisibility = Literal["public", "private"]
ProjectStatus = Literal["active", "stale", "archived"]
ProjectContextType = Literal["current_work", "next_step", "summary", "note", "insight"]
ActionStatus = Literal["started", "completed", "failed"]

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class ProjectSkillOut(StrictModel):
    name: str
    path: str = ""
    description: str = ""


class ProjectContextOut(StrictModel):
    """Latest-context entry value (no id — latest only)."""
    content: str
    source: str
    created_at: dt.datetime


class ProjectContextEntryOut(StrictModel):
    """Full context entry (with id — used by /context/ list)."""
    id: int
    context_type: ProjectContextType
    content: str
    source: str
    created_at: dt.datetime


class ProjectContextCreateIn(StrictModel):
    context_type: ProjectContextType
    content: str = Field(min_length=1)
    source: str = Field(min_length=1, max_length=100)


class ProjectActionLatestOut(StrictModel):
    status: ActionStatus
    started_at: dt.datetime
    completed_at: dt.datetime | None = None


class ProjectActionOut(StrictModel):
    id: int
    skill_name: str
    session_id: str = ""
    status: ActionStatus
    started_at: dt.datetime
    completed_at: dt.datetime | None = None
    duration_ms: int | None = None
    notes: str = ""
    created_at: dt.datetime


class ProjectActionCreateIn(StrictModel):
    skill_name: str = Field(min_length=1, max_length=100)
    session_id: str = ""
    status: ActionStatus = "started"
    started_at: dt.datetime
    completed_at: dt.datetime | None = None
    duration_ms: int | None = None
    notes: str = ""


class ProjectActionSummaryOut(StrictModel):
    skill_name: str
    status: ActionStatus
    started_at: dt.datetime
    completed_at: dt.datetime | None = None


class ProjectListOut(StrictModel):
    id: int
    name: str
    slug: str
    repo_url: str = ""
    deploy_url: str = ""
    visibility: ProjectVisibility
    status: ProjectStatus
    skills: list[ProjectSkillOut]
    latest_context: dict[str, ProjectContextOut]
    latest_actions: dict[str, ProjectActionLatestOut]
    insight_count: int = Field(ge=0)
    walkthrough_count: int = Field(ge=0, default=0)  # added by PR #41
    created_by_email: str | None = None  # who registered it (null for pre-attribution rows)
    created_at: dt.datetime
    updated_at: dt.datetime


class ProjectDetailOut(ProjectListOut):
    """Detail view — same shape as list today. Diverge here if needed."""
    pass


class ProjectCreateIn(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50, pattern=SLUG_PATTERN)
    repo_url: str = ""
    deploy_url: str = ""
    visibility: ProjectVisibility = "public"
    status: ProjectStatus = "active"
    skills: list[ProjectSkillOut] = Field(default_factory=list)


class ProjectPatchIn(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    repo_url: str | None = None
    deploy_url: str | None = None
    visibility: ProjectVisibility | None = None
    status: ProjectStatus | None = None
    skills: list[ProjectSkillOut] | None = None


class ProjectSlugOut(StrictModel):
    """Slim machine-readable shape from /api/projects/slugs/."""
    slug: str
    name: str
    status: ProjectStatus
    visibility: ProjectVisibility
    workspace: str | None


class ProjectContextLatestOut(StrictModel):
    contexts: dict[str, ProjectContextOut]


class BatchContextIn(StrictModel):
    """Body of POST /api/projects/batch-context/.

    Each value is a list of ProjectContextCreateIn shapes.
    """
    updates: dict[str, list[ProjectContextCreateIn]]


class BatchActionsIn(StrictModel):
    """Body of POST /api/projects/batch-actions/."""
    updates: dict[str, list[ProjectActionCreateIn]]


# --- Insights ----------------------------------------------------------


class InsightOut(StrictModel):
    id: int
    project_slug: str
    project_name: str
    content: str
    source: str
    created_at: dt.datetime


class InsightsClearIn(StrictModel):
    """Body of POST /api/insights/clear/.

    All fields optional. Provided filters are AND-combined to narrow which
    insights are deleted. A body with no filters clears ALL insights.
    """
    source: str | None = None
    category: str | None = None
    project: str | None = None  # project slug
    older_than_days: int | None = None


class InsightsClearOut(StrictModel):
    cleared: int


class InsightDismissOut(StrictModel):
    dismissed: int


class InsightsDismissIn(StrictModel):
    """Body of POST /api/insights/dismiss."""
    ids: list[int]


class InsightsDismissOut(StrictModel):
    dismissed: list[int]
