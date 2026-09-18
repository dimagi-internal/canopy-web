"""`skill_history` / `skill_revision_diff`, driven through the mounted server."""
from __future__ import annotations

import contextlib
import datetime as dt
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id=str(user.pk), scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _call(name, **kw):
    return async_to_sync(mcp.call_tool)(name, kw).structured_content


@pytest.fixture
def world():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    outsider = User.objects.create_user(username="x", email="x@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    a = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                             repo_url="https://github.com/dimagi-internal/ace")
    c = SkillHistoryCommit.objects.create(agent=a, sha="c" * 40, subject="fix(idea-to-pdd): read comments",
                                          body="Reviewers were being transcribed by hand.",
                                          committed_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC))
    SkillRevision.objects.create(commit=c, skill="idea-to-pdd", lines_after=1333, added=15, deleted=0)
    SkillHistorySync.objects.create(agent=a, last_state="ok", checks={"idea-to-pdd-eval": "idea-to-pdd"})
    return owner, outsider, a


def test_both_tools_are_registered_on_the_mounted_server():
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"skill_history", "skill_revision_diff"} <= names


def test_skill_history_returns_bodies_and_checking_skills(world):
    owner, _, _ = world
    with as_user(owner):
        out = _call("skill_history", agent="ace", skill="idea-to-pdd")
    assert out["revisions"][0]["body"] == "Reviewers were being transcribed by hand."
    assert out["checked_by"] == ["idea-to-pdd-eval"]


def test_an_outsider_learns_nothing(world):
    _, outsider, _ = world
    with as_user(outsider):
        out = _call("skill_history", agent="ace")
    assert out == {"error": "agent 'ace' not found"}


def test_diff_is_fetched_live_with_the_owners_grant_and_truncated(world):
    owner, _, agent = world
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"files": [
        {"filename": "skills/idea-to-pdd/SKILL.md", "patch": "+x\n" * 20000},
        {"filename": "README.md", "patch": "+y"},
    ]}
    with as_user(owner), \
         mock.patch.object(skill_history.github_app, "access_token_for", return_value="tok") as tok, \
         mock.patch.object(skill_history.requests, "get", return_value=resp) as get:
        out = _call("skill_revision_diff", agent="ace", sha="c" * 40, skill="idea-to-pdd")
    tok.assert_called_once_with(agent.owner)
    assert get.call_args.args[0] == "https://api.github.com/repos/dimagi-internal/ace/commits/" + "c" * 40
    assert out["truncated"] is True
    assert len(out["patch"].encode()) <= 20 * 1024


def test_a_bad_sha_is_refused_before_any_github_call(world):
    owner, _, _ = world
    with as_user(owner), \
         mock.patch.object(skill_history.github_app, "access_token_for") as tok, \
         mock.patch.object(skill_history.requests, "get") as get:
        out = _call("skill_revision_diff", agent="ace", sha="../../user", skill="idea-to-pdd")
    assert "error" in out
    tok.assert_not_called()
    get.assert_not_called()


def test_a_non_github_repo_url_is_refused_before_any_github_call(world):
    owner, _, agent = world
    agent.repo_url = "https://example.com/dimagi-internal/ace"
    agent.save(update_fields=["repo_url"])
    with as_user(owner), \
         mock.patch.object(skill_history.github_app, "access_token_for") as tok, \
         mock.patch.object(skill_history.requests, "get") as get:
        out = _call("skill_revision_diff", agent="ace", sha="c" * 40, skill="idea-to-pdd")
    assert "error" in out
    tok.assert_not_called()
    get.assert_not_called()
