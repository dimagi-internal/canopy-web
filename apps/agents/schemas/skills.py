from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


# ---- Skill catalog ----
class AgentSkillIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    url: str = Field(default="", max_length=500)
    improvement_note: str = ""
    # Fail closed: a publish that predates these fields marks nothing launchable.
    launchable: bool = False
    args_hint: str = Field(default="", max_length=120)


class AgentSkillCatalogIn(StrictModel):
    """Full replacement of the agent's skill catalog."""

    skills: list[AgentSkillIn] = Field(default_factory=list)


class AgentSkillOut(StrictModel):
    id: int
    agent_slug: str
    name: str
    description: str
    url: str
    improvement_note: str
    launchable: bool
    args_hint: str
    updated_at: dt.datetime


# ---- skill history (pulled from the agent's repo) ----
class SkillHistoryGroupOut(StrictModel):
    title: str
    kind: Literal["phase", "agent", "none"]
    num: str
    skills: list[str]


class SkillHistoryCommitOut(StrictModel):
    sha: str
    date: str
    subject: str


class SkillHistorySkillOut(StrictModel):
    name: str
    # Each revision is [commit_index, lines_after, added, deleted]; commit_index
    # indexes SkillHistoryOut.commits. Tuples rather than objects because ACE
    # has ~2,300 of them and the page downloads them all.
    revisions: list[list[int]]


class SkillHistoryOut(StrictModel):
    agent: str
    repo_url: str
    head_sha: str
    synced_at: dt.datetime | None
    synced_with: str
    last_error: str
    credential_state: Literal["ok", "no_repo", "no_owner", "owner_not_connected", "repo_not_granted"]
    # The agent owner's display name (or email), "" when there is no owner.
    # The credential is the owner's, so the page names who has to act.
    owner_name: str
    # Whether the caller is the owner (only they can connect the GitHub grant).
    viewer_is_owner: bool
    # Whether the caller may force a sync (editor or owner in the workspace).
    viewer_can_sync: bool
    # GitHub's installation screen for the canopy-agents App, "" when the
    # deployment has no App configured.
    install_url: str
    groups: list[SkillHistoryGroupOut]
    checks: dict[str, str]
    present: list[str]
    commits: list[SkillHistoryCommitOut]
    skills: list[SkillHistorySkillOut]
