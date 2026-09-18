from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def ws_and_users():
    owner = User.objects.create_user(username="o", email="o@dimagi.com", first_name="Olive", last_name="Owner")
    viewer = User.objects.create_user(username="v", email="v@dimagi.com")
    outsider = User.objects.create_user(username="x", email="x@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    return ws, owner, viewer, outsider


@pytest.fixture
def editor(ws_and_users):
    ws = ws_and_users[0]
    u = User.objects.create_user(username="e", email="e@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.EDITOR)
    return u


@pytest.fixture
def agent(ws_and_users):
    ws, owner, _, _ = ws_and_users
    a = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, repo_url="https://github.com/o/ace")
    c1 = SkillHistoryCommit.objects.create(agent=a, sha="a" * 40, subject="feat: alpha",
                                           committed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc))
    c2 = SkillHistoryCommit.objects.create(agent=a, sha="b" * 40, subject="fix: alpha",
                                           committed_at=dt.datetime(2026, 4, 5, tzinfo=dt.timezone.utc))
    SkillRevision.objects.create(commit=c1, skill="alpha", lines_after=10, added=10, deleted=0)
    SkillRevision.objects.create(commit=c2, skill="alpha", lines_after=12, added=2, deleted=0)
    SkillHistorySync.objects.create(agent=a, head_sha="b" * 40, synced_at=timezone.now(), synced_with="o-gh",
                                    last_state="ok", groups=[{"title": "One", "kind": "phase", "num": "01", "skills": ["alpha"]}],
                                    checks={}, present=["alpha"])
    return a


def test_a_member_reads_the_compact_payload(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    r = client.get("/api/agents/ace/skill-history/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["credential_state"] == "ok"
    assert body["synced_with"] == "o-gh"
    assert body["commits"] == [
        {"sha": "a" * 40, "date": "2026-04-01", "subject": "feat: alpha"},
        {"sha": "b" * 40, "date": "2026-04-05", "subject": "fix: alpha"},
    ]
    assert body["skills"] == [{"name": "alpha", "revisions": [[0, 10, 10, 0], [1, 12, 2, 0]]}]
    assert body["groups"][0]["title"] == "One"


def test_reading_a_fresh_history_does_not_sync(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    with mock.patch.object(skill_history, "sync") as s:
        client.get("/api/agents/ace/skill-history/")
    s.assert_not_called()


def test_reading_a_stale_history_syncs_first(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    SkillHistorySync.objects.filter(agent=agent).update(synced_at=timezone.now() - timezone.timedelta(hours=2))
    client.force_login(viewer)
    with mock.patch.object(skill_history, "sync") as s:
        client.get("/api/agents/ace/skill-history/")
    s.assert_called_once()


def test_a_non_member_gets_404(client, ws_and_users, agent):
    _, _, _, outsider = ws_and_users
    client.force_login(outsider)
    assert client.get("/api/agents/ace/skill-history/").status_code == 404


def test_a_viewer_cannot_force_a_sync(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    assert client.post("/api/agents/ace/skill-history/sync").status_code == 403


def test_an_owner_can_force_a_sync(client, ws_and_users, agent):
    _, owner, _, _ = ws_and_users
    client.force_login(owner)
    with mock.patch.object(skill_history, "sync") as s:
        r = client.post("/api/agents/ace/skill-history/sync")
    assert r.status_code == 200
    s.assert_called_once_with(agent, force=True)


def test_skill_revisions_filters_and_caps(agent):
    out = skill_history.skill_revisions(agent, skill="alpha", group=None, since=None, until=None, limit=1)
    assert out["truncated"] is True
    assert out["revisions"][0]["subject"] == "fix: alpha"  # newest first
    assert out["revisions"][0]["line_change"] == 2


def test_the_payload_says_who_must_act(client, ws_and_users, agent, settings):
    """The credential is the OWNER's: the page names them, offers Connect only
    to them, and offers Sync only to editors and owners."""
    _, owner, viewer, _ = ws_and_users
    settings.GITHUB_APP_CLIENT_ID = "cid"
    settings.GITHUB_APP_CLIENT_SECRET = "sec"
    settings.GITHUB_APP_SLUG = "canopy-agents"

    client.force_login(viewer)
    body = client.get("/api/agents/ace/skill-history/").json()
    assert body["owner_name"] == "Olive Owner"
    assert body["viewer_is_owner"] is False
    assert body["viewer_can_sync"] is False
    assert body["install_url"] == "https://github.com/apps/canopy-agents/installations/new"

    client.force_login(owner)
    body = client.get("/api/agents/ace/skill-history/").json()
    assert body["viewer_is_owner"] is True
    assert body["viewer_can_sync"] is True


def test_an_editor_can_sync_but_is_not_the_owner(client, agent, editor):
    client.force_login(editor)
    body = client.get("/api/agents/ace/skill-history/").json()
    assert body["viewer_can_sync"] is True
    assert body["viewer_is_owner"] is False


def test_no_install_url_without_a_configured_app(client, ws_and_users, agent, settings):
    settings.GITHUB_APP_CLIENT_ID = ""
    settings.GITHUB_APP_CLIENT_SECRET = ""
    client.force_login(ws_and_users[2])
    assert client.get("/api/agents/ace/skill-history/").json()["install_url"] == ""


def test_owner_name_falls_back_to_email_and_is_empty_without_an_owner(client, ws_and_users, agent):
    _, owner, viewer, _ = ws_and_users
    owner.first_name = owner.last_name = ""
    owner.save()
    client.force_login(viewer)
    assert client.get("/api/agents/ace/skill-history/").json()["owner_name"] == "o@dimagi.com"
    agent.owner = None
    agent.save()
    assert client.get("/api/agents/ace/skill-history/").json()["owner_name"] == ""


def test_a_failed_attempt_is_not_retried_on_the_next_read(client, ws_and_users, agent):
    """A failure never moves synced_at, so staleness alone would re-sync (a
    token refresh plus a clone) on every page load for an agent whose repo is
    not granted. The attempt stamp debounces it; the Sync button still forces."""
    _, _, viewer, _ = ws_and_users
    SkillHistorySync.objects.filter(agent=agent).update(
        synced_at=timezone.now() - timezone.timedelta(hours=2),
        last_attempt_at=timezone.now() - timezone.timedelta(minutes=5),
        last_state="repo_not_granted",
    )
    client.force_login(viewer)
    with mock.patch.object(skill_history, "sync") as s:
        client.get("/api/agents/ace/skill-history/")
    s.assert_not_called()


def test_a_real_failed_attempt_debounces_the_next_read(client, ws_and_users, agent):
    """End to end through the real sync: the first read attempts (and fails
    for want of a grant), the second read within the hour does not."""
    from apps.tokens.github_app import GitHubAuthError

    _, _, viewer, _ = ws_and_users
    SkillHistorySync.objects.filter(agent=agent).update(synced_at=timezone.now() - timezone.timedelta(hours=2))
    client.force_login(viewer)
    with mock.patch.object(skill_history.github_app, "access_token_for",
                           side_effect=GitHubAuthError("no GitHub connection")) as tok:
        client.get("/api/agents/ace/skill-history/")
        client.get("/api/agents/ace/skill-history/")
    assert tok.call_count == 1
    assert SkillHistorySync.objects.get(agent=agent).last_state == "owner_not_connected"


def test_an_unexpected_sync_failure_still_serves_the_stored_history(client, ws_and_users, agent):
    """A deterministic bug in the sync must not turn every read into a 500."""
    _, _, viewer, _ = ws_and_users
    SkillHistorySync.objects.filter(agent=agent).update(synced_at=timezone.now() - timezone.timedelta(hours=2))
    client.force_login(viewer)
    with mock.patch.object(skill_history, "_clone_and_read", side_effect=RuntimeError("boom")), \
         mock.patch.object(skill_history.github_app, "access_token_for", return_value="tok"):
        r = client.get("/api/agents/ace/skill-history/")
    assert r.status_code == 200, r.content
    assert [c["sha"] for c in r.json()["commits"]] == ["a" * 40, "b" * 40]
    row = SkillHistorySync.objects.get(agent=agent)
    assert row.sync_started_at is None  # the claim did not outlive the attempt
    assert row.last_attempt_at is not None  # and the attempt is debounced
