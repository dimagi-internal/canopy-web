"""/api/agents/{slug}/skills… — an agent's skill catalog and its history."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.tokens.github_app import GitHubAuthError, GitHubNotConfigured

from . import services
from . import skill_history as history
from .gates import (
    agent_for_read,
    agent_for_work,
)
from .schemas import (
    AgentSkillCatalogIn,
    AgentSkillOut,
    CountOut,
    SkillHistoryOut,
)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])


# ---- skill catalog ----
@router.get("/{slug}/skills/", response=list[AgentSkillOut], summary="List the agent's skill catalog",)
def list_skills(request: HttpRequest, slug: str) -> list[AgentSkillOut]:
    agent = agent_for_read(request, slug)
    return [AgentSkillOut.model_validate(s) for s in services.list_skills(agent)]


@router.put("/{slug}/skills/", response=CountOut, summary="Replace the agent's skill catalog",)
def replace_skills(request: HttpRequest, slug: str, payload: AgentSkillCatalogIn) -> CountOut:
    agent = agent_for_work(request, slug)
    count = services.replace_skills(agent, payload.skills)
    return CountOut(count=count)


# ---- skill history ----
# Reading syncs first when the stored history is over an hour old: opening the
# page is the trigger (no scheduler exists, and a stale history costs one
# click). The sync itself is debounced per agent, so many open tabs clone once.
@router.get("/{slug}/skill-history/", response=SkillHistoryOut,
            summary="How the agent's skills changed, from its repository's history")
def get_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = agent_for_read(request, slug)
    # Due = stale AND no attempt in the last hour: a FAILED attempt never moves
    # synced_at, so without the attempt debounce a repo_not_granted agent would
    # refresh the owner's token and clone on every page load. An owner who has
    # just fixed access presses Sync, which forces.
    if history.due_for_auto_sync(agent):
        try:
            history.sync(agent)
        except Exception:
            # A read must not 500 because the refresh behind it broke — serve
            # what is stored. The attempt is already stamped (see `_claim`),
            # so a deterministic failure is not retried on every load either.
            logger.exception("skill history auto-sync failed for agent %s", agent.slug)
    return SkillHistoryOut(**history.history_payload(agent, request.user))


@router.post("/{slug}/skill-history/sync", response=SkillHistoryOut,
             summary="Re-read the agent's skill history from its repository now")
def sync_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = agent_for_work(request, slug)
    history.sync(agent, force=True)
    return SkillHistoryOut(**history.history_payload(agent, request.user))


@router.get("/{slug}/skill-history/revisions", response=dict[str, Any],
            summary="Skill revisions, newest first, with their commit messages")
def skill_history(
    request: HttpRequest, slug: str, skill: str | None = None, group: str | None = None,
    since: dt.date | None = None, until: dt.date | None = None, limit: int | None = None,
    commit: str | None = None,
) -> dict:
    """How an agent's skills changed, from its repository's git history.

    Returns revisions newest first: date, skill, commit subject AND body (the
    body is where the reason for a change is usually spelled out), lines after,
    and the line change. For a single skill it also names the QA/eval skills
    that check it (`checked_by`) or the skill it checks (`checks`).

    Filters: `skill` name, `group` title (a phase or agent from the History
    page), `commit` (a sha or sha prefix, 7 to 64 hex characters),
    `since` / `until` dates.

    Returns the 25 most recent matches by default; `limit` raises that, capped
    at 300. In a list each body is summarised to its first 700 characters and
    `body_truncated` says so — ask for that one `commit` to read it whole. A
    mature skill has hundreds of revisions and its bodies run to thousands of
    words each, so narrow with `skill`, `since`/`until` or `commit` rather than
    asking for everything.

    On the History page, read `current_page` first: it says which skill, group
    or commit is selected and the date being looked at. A selected commit's sha
    is `commit`; the page's `as_of` date is `until`.
    """
    agent = agent_for_read(request, slug)
    try:
        return history.skill_revisions(
            agent, skill=skill, group=group, since=since, until=until, limit=limit, commit=commit,
        )
    except history.SyncError as e:  # a malformed commit
        raise HttpError(422, str(e)) from e


@router.get("/{slug}/skill-history/diff", response=dict[str, Any],
            summary="The change one commit made to one skill, as a unified diff")
def skill_revision_diff(request: HttpRequest, slug: str, sha: str, skill: str) -> dict:
    """The exact change one commit made to one skill's SKILL.md, as a unified diff.

    Fetched live from GitHub (not stored), truncated to 20 KB. Use it when a
    commit message does not say enough about what actually changed.
    """
    import requests

    agent = agent_for_read(request, slug)
    try:
        return history.revision_diff(agent, sha, skill)
    except history.SyncError as e:
        raise HttpError(422, str(e)) from e
    except (GitHubAuthError, GitHubNotConfigured) as e:
        raise HttpError(409, str(e)) from e
    except requests.RequestException as e:
        raise HttpError(502, f"GitHub: {e}") from e
