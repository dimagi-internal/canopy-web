"""An agent's skill history, as MCP tools — the backing tool for the History page.

The page declares `backing_tool: skill_history` with the selected skill, group or
commit; these resolve that selection with the caller's own access applied. The
tenant gate is the one authorizer (`workspace_slugs_for_user_id`), borrowed as
`list_items` borrows it — never a second implementation.
"""
from __future__ import annotations

import datetime as dt

import requests
from asgiref.sync import sync_to_async

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


def _visible_agent(user_id, slug: str):
    from apps.agents.models import Agent
    from apps.workspaces import services as wsvc

    return Agent.objects.filter(slug=slug, workspace_id__in=wsvc.workspace_slugs_for_user_id(user_id)).first()


def _history(user_id, agent, skill, group, since, until, limit, commit):
    from apps.agents import skill_history

    a = _visible_agent(user_id, agent)
    if a is None:
        return {"error": f"agent '{agent}' not found"}
    try:
        parse = lambda s: dt.date.fromisoformat(s) if s else None  # noqa: E731
        return skill_history.skill_revisions(
            a, skill=skill, group=group, since=parse(since), until=parse(until), limit=limit,
            commit=commit,
        )
    except ValueError as e:  # a malformed since/until
        return {"error": f"dates must be YYYY-MM-DD: {e}"}
    except skill_history.SyncError as e:  # a malformed commit
        return {"error": str(e)}


def _diff(user_id, agent, sha, skill):
    from apps.agents import skill_history
    from apps.tokens.github_app import GitHubAuthError, GitHubNotConfigured

    a = _visible_agent(user_id, agent)
    if a is None:
        return {"error": f"agent '{agent}' not found"}
    try:
        return skill_history.revision_diff(a, sha, skill)
    except (skill_history.SyncError, GitHubAuthError, GitHubNotConfigured, requests.RequestException) as e:
        return {"error": str(e)}


@mcp.tool
async def skill_history(
    agent: str,
    skill: str | None = None,
    group: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    commit: str | None = None,
) -> dict:
    """How an agent's skills changed, from its repository's git history.

    Returns revisions newest first: date, skill, commit subject AND body (the
    body is where the reason for a change is usually spelled out), lines after,
    and the line change. For a single skill it also names the QA/eval skills
    that check it (`checked_by`) or the skill it checks (`checks`).

    Filters: `skill` name, `group` title (a phase or agent from the History
    page), `commit` (a sha or sha prefix, 7 to 64 hex characters),
    `since` / `until` as YYYY-MM-DD.

    Returns the 25 most recent matches by default; `limit` raises that, capped
    at 300. In a list each body is summarised to its first 700 characters and
    `body_truncated` says so — ask for that one `commit` to read it whole. A
    mature skill has hundreds of revisions and its bodies run to thousands of
    words each, so narrow with `skill`, `since`/`until` or `commit` rather than
    asking for everything.

    On the History page, read `current_page` first: it says which skill, group
    or commit the user has selected and the date they are looking at. Its
    selection maps onto these parameters directly — a selected commit's sha
    (`visible_ids` / the `commit` filter) is `commit`, and the page's `as_of`
    date filter is `until`.
    """
    user_id = current_user_id()
    out = await sync_to_async(_history, thread_sensitive=True)(user_id, agent, skill, group, since, until, limit, commit)
    await write_audit(user_id=user_id, tool="skill_history",
                      args_summary=f"agent={agent} skill={skill} group={group} commit={commit} -> {len(out.get('revisions', []))}")
    return out


@mcp.tool
async def skill_revision_diff(agent: str, sha: str, skill: str) -> dict:
    """The exact change one commit made to one skill's SKILL.md, as a unified diff.

    Fetched live from GitHub (not stored), truncated to 20 KB. Use it when a
    commit message does not say enough about what actually changed.
    """
    user_id = current_user_id()
    out = await sync_to_async(_diff, thread_sensitive=True)(user_id, agent, sha, skill)
    await write_audit(
        user_id=user_id, tool="skill_revision_diff", args_summary=f"agent={agent} sha={sha[:12]} skill={skill}"
    )
    return out
