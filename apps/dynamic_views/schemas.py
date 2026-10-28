from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import Field

from apps.common.schemas import StrictModel


class DynamicViewAgentTask(StrictModel):
    agent: str = Field(description="An agent's slug, in the view's workspace.")
    name: str = Field(description="The task name the code passes, e.g. `gmail-triage.review`.")


class DynamicViewSummaryOut(StrictModel):
    id: int
    workspace: str
    project: str | None = Field(None, description="The project's handle, or null for a workspace view.")
    project_name: str | None = None
    slug: str
    title: str
    description: str
    version: int
    url: str | None = Field(None, description="The view's page in canopy.")
    updated_by_email: str | None = None
    updated_at: dt.datetime


class DynamicViewOut(DynamicViewSummaryOut):
    code: str
    sources: list[dict[str, Any]]
    agent_tasks: list[DynamicViewAgentTask] = Field(
        default_factory=list, description="The agent tasks the view's code may start, as its viewer.")
    created_by_email: str | None = None
    created_at: dt.datetime


class DynamicViewIn(StrictModel):
    workspace: str = Field("", description="Defaults to the pinned workspace.")
    project: str | None = Field(None, description="A project handle in that workspace; omit for a workspace view.")
    title: str
    slug: str = Field("", description="Defaults to one made from the title.")
    description: str = ""
    code: str = Field(description="JSX defining `function View(props)`. See GET /api/dynamic-views/guide/.")
    sources: list[dict[str, Any]] = Field(default_factory=list,
                                          description="Catalog reads made for the view: [{alias, model, params?, path?}].")
    agent_tasks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The agent tasks the code may start with canopy.runAgentTask: [{agent, name}]. Each agent "
                    "must be in the view's workspace and one you may give tasks to yourself.")
    note: str = ""


class DynamicViewPatch(StrictModel):
    title: str | None = None
    slug: str | None = None
    description: str | None = None
    code: str | None = None
    sources: list[dict[str, Any]] | None = None
    agent_tasks: list[dict[str, Any]] | None = None
    project: str | None = Field(None, description="A project handle; an empty string moves it to the workspace.")
    note: str = ""


class DynamicViewRevisionOut(StrictModel):
    version: int
    title: str
    note: str
    changed_by_email: str | None = None
    changed_at: dt.datetime
    code: str
    sources: list[dict[str, Any]]
    agent_tasks: list[DynamicViewAgentTask] = Field(default_factory=list)


class DynamicViewRollbackIn(StrictModel):
    version: int


class CatalogModelOut(StrictModel):
    name: str
    path: str
    description: str
    params: list[str]
    path_params: list[str]


class DynamicViewGuideOut(StrictModel):
    contract: str = Field(description="How to write a view: props, the canopy API, the kit, the rules.")
    catalog: list[CatalogModelOut]
    example: str = Field(description="A complete, working view to start from.")
    example_sources: list[dict[str, Any]] = Field(description="The sources the example reads.")
