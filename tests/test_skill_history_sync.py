# tests/test_skill_history_sync.py
"""Syncing an agent's skill history from its repo.

The repo is a real local git repository and `repo_url` is a bare
`https://github.com/<owner>/<repo>` URL (the only shape `_validate_repo_url`
accepts) rewritten to that local repo via a per-test `GIT_CONFIG_GLOBAL`
(`insteadOf`), so the clone, the log and the frontmatter read are the real
commands — only the GitHub credential is faked, at `access_token_for`, the one
seam the spec names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
import requests
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.tokens.github_app import GitHubAuthError
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
TOKEN = "ghu_SECRETTOKENVALUE"
GITHUB_URL = "https://github.com/o/agent-repo"


def _git(repo: Path, *args: str, date: str = "2026-04-01T10:00:00+00:00") -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "agent-repo"
    (r / "skills/alpha").mkdir(parents=True)
    (r / "agents").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(r))
    (r / "skills/alpha/SKILL.md").write_text("a\n" * 10)
    (r / "agents/one.md").write_text("---\nname: one\nphase_ordinal: 1\nphase_display: One\n"
                                     "skills:\n  - { name: alpha, eval_skill: alpha-eval }\n---\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat: alpha")
    (r / "skills/alpha-eval").mkdir()
    (r / "skills/alpha-eval/SKILL.md").write_text("e\n" * 4)
    (r / "skills/alpha/SKILL.md").write_text("a\n" * 12)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "fix(alpha): two more lines", date="2026-04-05T10:00:00+00:00")
    return r


@pytest.fixture
def git_rewrite(tmp_path, repo, monkeypatch):
    """Redirect the fake `https://github.com/o/agent-repo` URL to the real
    local repo, the way a developer's `~/.gitconfig` `insteadOf` points a host
    at a mirror. Also allows the `file` protocol: production's `_clone` passes
    `-c protocol.allow=never`, which blocks `file` by default, but a
    *specific* key (`protocol.file.allow`) always wins over that *generic*
    one regardless of which config source set it — so this global config,
    layered on top of the command line's `-c`, is what makes the local
    rewrite reachable in tests without loosening anything in production.
    """
    cfg = tmp_path / "gitconfig-happy"
    cfg.write_text(
        f'[url "{repo}"]\n\tinsteadOf = {GITHUB_URL}\n'
        '[protocol "file"]\n\tallow = always\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    return cfg


@pytest.fixture
def owner():
    return User.objects.create_user(username="own", email="own@dimagi.com")


@pytest.fixture
def agent(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                repo_url=GITHUB_URL, repo_ref="main")


@pytest.fixture
def granted(git_rewrite):
    with mock.patch.object(skill_history.github_app, "access_token_for", return_value=TOKEN) as m, \
         mock.patch.object(skill_history, "_github_login", return_value="own-gh"):
        yield m


def _unreachable_rewrite(tmp_path, monkeypatch, url: str) -> None:
    """Point `url` at a local path that does not exist, so a clone of it
    fails the same way an ungranted repo would — locally, with no network
    call and no dependency on a real unreachable host."""
    cfg = tmp_path / "gitconfig-unreachable"
    cfg.write_text(
        f'[url "{tmp_path / "does-not-exist"}"]\n\tinsteadOf = {url}\n'
        '[protocol "file"]\n\tallow = always\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


def test_sync_stores_commits_revisions_groups_and_checks(agent, granted):
    row = skill_history.sync(agent)

    assert row.last_state == "ok" and row.last_error == ""
    assert row.synced_with == "own-gh"
    assert len(row.head_sha) == 40
    assert [c.subject for c in SkillHistoryCommit.objects.filter(agent=agent)] == ["feat: alpha", "fix(alpha): two more lines"]
    alpha = list(SkillRevision.objects.filter(commit__agent=agent, skill="alpha").order_by("commit__committed_at"))
    assert [(r.lines_after, r.added, r.deleted) for r in alpha] == [(10, 10, 0), (12, 2, 0)]
    assert row.groups == [{"title": "One", "kind": "phase", "num": "01", "skills": ["alpha"]}]
    assert row.checks == {"alpha-eval": "alpha"}
    assert sorted(row.present) == ["alpha", "alpha-eval"]


def test_the_credential_is_the_owners_never_the_viewers(agent, granted):
    skill_history.sync(agent)
    granted.assert_called_once_with(agent.owner)


def test_resync_replaces_wholesale(agent, granted):
    skill_history.sync(agent)
    skill_history.sync(agent, force=True)
    assert SkillHistoryCommit.objects.filter(agent=agent).count() == 2


def test_a_sync_in_flight_is_not_started_twice(agent, granted):
    SkillHistorySync.objects.create(agent=agent, sync_started_at=timezone.now())
    with mock.patch.object(skill_history, "_clone_and_read") as clone:
        skill_history.sync(agent)
    clone.assert_not_called()


def test_a_forced_sync_still_honours_an_in_flight_claim(agent, granted):
    """Force skips only the "is it due?" check. Two overlapping syncs would
    each delete-and-reinsert the commits and collide on the unique constraint."""
    SkillHistorySync.objects.create(agent=agent, sync_started_at=timezone.now())
    with mock.patch.object(skill_history, "_clone_and_read") as clone:
        skill_history.sync(agent, force=True)
    clone.assert_not_called()


def test_an_unforced_sync_is_skipped_when_not_due(agent, granted):
    skill_history.sync(agent)
    with mock.patch.object(skill_history, "_clone_and_read") as clone:
        skill_history.sync(agent)
    clone.assert_not_called()


def test_every_attempt_is_stamped_even_a_failed_one(agent):
    with mock.patch.object(skill_history.github_app, "access_token_for",
                           side_effect=GitHubAuthError("no GitHub connection")):
        row = skill_history.sync(agent)
    assert row.last_attempt_at is not None
    assert row.synced_at is None
    assert not skill_history.due_for_auto_sync(agent)


def test_a_stale_claim_older_than_the_window_is_taken_over(agent, granted):
    SkillHistorySync.objects.create(agent=agent, sync_started_at=timezone.now() - timezone.timedelta(seconds=120))
    row = skill_history.sync(agent)
    assert row.last_state == "ok"


@pytest.mark.parametrize("setup,state", [
    (lambda a: setattr(a, "repo_url", ""), "no_repo"),
    (lambda a: setattr(a, "owner", None), "no_owner"),
])
def test_states_known_before_cloning(agent, setup, state):
    setup(agent)
    agent.save()
    row = skill_history.sync(agent)
    assert row.last_state == state
    assert skill_history.credential_state(agent) == state


def test_owner_not_connected(agent):
    with mock.patch.object(skill_history.github_app, "access_token_for", side_effect=GitHubAuthError("no GitHub connection")):
        row = skill_history.sync(agent)
    assert row.last_state == "owner_not_connected"


@pytest.mark.parametrize("bad_url", [
    "https://gitlab.com/o/agent-repo",   # not github.com
    "/nonexistent/repo",                 # local filesystem path, no scheme
    "-x",                                 # would be read as a git option
    "file:///etc/passwd",                 # file:// scheme
    "https://user:pw@github.com/o/repo",  # embedded credentials
])
def test_an_invalid_repo_url_never_touches_git_or_github(agent, bad_url):
    agent.repo_url = bad_url
    agent.save()
    with mock.patch.object(skill_history, "_clone_and_read") as clone, \
         mock.patch.object(skill_history.github_app, "access_token_for") as token_call:
        row = skill_history.sync(agent)
    clone.assert_not_called()
    token_call.assert_not_called()
    assert row.last_state == "repo_not_granted"
    assert skill_history.credential_state(agent) == "repo_not_granted"


def test_a_transient_network_failure_fetching_the_token_does_not_raise(agent):
    with mock.patch.object(skill_history.github_app, "access_token_for",
                           side_effect=requests.ConnectionError("connection refused")):
        row = skill_history.sync(agent)
    assert row.last_state == "ok"
    assert row.last_error


def test_a_repo_the_grant_cannot_reach_is_repo_not_granted_and_keeps_the_old_history(agent, granted, tmp_path, monkeypatch):
    skill_history.sync(agent)
    unreachable_url = "https://github.com/o/unreachable-repo"
    _unreachable_rewrite(tmp_path, monkeypatch, unreachable_url)
    agent.repo_url = unreachable_url
    agent.save()
    row = skill_history.sync(agent, force=True)
    assert row.last_state == "repo_not_granted"
    assert SkillHistoryCommit.objects.filter(agent=agent).count() == 2  # previous snapshot intact


def test_the_token_never_reaches_the_stored_error(agent, granted, tmp_path, monkeypatch):
    unreachable_url = "https://github.com/o/still-unreachable"
    _unreachable_rewrite(tmp_path, monkeypatch, unreachable_url)
    agent.repo_url = unreachable_url
    agent.save()
    row = skill_history.sync(agent, force=True)
    assert TOKEN not in row.last_error
    assert row.last_error  # something human-readable was recorded


def test_sync_marks_the_resource_dirty(agent, granted):
    with mock.patch.object(skill_history, "mark_dirty") as dirty:
        skill_history.sync(agent)
    dirty.assert_called_once_with("skill-history://ace")


def test_staleness(agent, granted):
    assert skill_history.is_stale(agent)
    skill_history.sync(agent)
    assert not skill_history.is_stale(agent)
