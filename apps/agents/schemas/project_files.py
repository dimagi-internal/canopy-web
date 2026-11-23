"""Schemas for a project's files (`apps/agents/api_project_files.py`)."""
from __future__ import annotations

from pydantic import ConfigDict, Field

from apps.common.schemas import StrictModel


class ProjectFileChangeIn(StrictModel):
    # A file's text is its bytes: a final newline is content, never trimmed like a form field.
    model_config = ConfigDict(str_strip_whitespace=False)

    path: str = Field(min_length=1, max_length=200)
    #: The file's new text, or null to delete it.
    content: str | None = Field(default=None, max_length=262_144)


class ProjectFilesDraftIn(StrictModel):
    #: The live revision these changes are made on: 0 while nothing is live yet.
    base: int = Field(ge=0)
    changes: list[ProjectFileChangeIn] = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=4000)


class ProjectFilesPublishIn(StrictModel):
    #: The draft's `root_sha256` as the publisher saw it: publishing a draft that changed
    #: since is 409 `changed`.
    expect_root: str = Field(default="", max_length=64)


class ProjectFilesRollbackIn(StrictModel):
    message: str = Field(default="", max_length=4000)


class ProjectFileEntryOut(StrictModel):
    path: str
    size: int
    sha256: str


class ProjectFileChangeOut(StrictModel):
    path: str
    change: str


class ProjectSkillFilesOut(StrictModel):
    name: str
    description: str
    files: list[str]


class ProjectFilesRevisionOut(StrictModel):
    number: int
    status: str
    message: str
    root_sha256: str
    commit: str
    author: str
    author_agent: str | None
    published_by: str | None
    published_at: str | None
    created_at: str
    files: list[ProjectFileEntryOut]
    #: From the revision it was made on (the live one, for a draft).
    changes: list[ProjectFileChangeOut]


class ProjectFilesOut(StrictModel):
    project: str
    live: ProjectFilesRevisionOut | None
    draft: ProjectFilesRevisionOut | None
    skills: list[ProjectSkillFilesOut]
    #: What the caller may do here, so a page shows only what will work.
    may_draft: bool
    may_publish: bool
    policy: list[str]


class ProjectFilesRevisionSummaryOut(StrictModel):
    number: int
    status: str
    message: str
    author: str
    published_by: str | None
    published_at: str | None
    created_at: str


class ProjectFileOut(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    path: str
    revision: int
    content: str
