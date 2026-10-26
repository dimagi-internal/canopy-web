"""Canopy is opt-in per repo, on the person's account (`ProjectOptIn`).

A repo its owner has not opted into is plain Claude Code: no repo turn routes
to it, no session or transcript is mirrored from it, no usage is kept from it.
An agent's own repo is canopy's regardless. docs/architecture/runners.md
§ Opted-in projects.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.events import usage
from apps.events.models import SettingChange
from apps.harness.models import ProjectOptIn, Runner
from apps.harness.session_sync import replace_reported_sessions
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = [pytest.mark.django_db, pytest.mark.explicit_opt_in]


@pytest.fixture
def jj():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture
def ws(jj):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def box(jj, ws):
    return Runner.objects.create(
        name="laptop", kind=Runner.EMDASH, host="jj-mac", owner=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web", "personal-site"]},
    )


def _opt_in(user, *, everything=False, projects=()):
    ProjectOptIn.objects.update_or_create(user=user, defaults={"everything": everything,
                                                               "projects": list(projects)})


def _fresh(runner):
    return Runner.objects.get(pk=runner.pk)


def _reported(task, project):
    return SimpleNamespace(emdash_task=task, project=project, status="running",
                           last_interacted_at=None, recent_messages=[])


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ---- the rule ---------------------------------------------------------------


def test_no_choice_is_nothing(box):
    assert box.observed_projects() == ["canopy-web", "personal-site"]
    assert box.project_names() == []


def test_everything_is_every_repo_the_box_holds(jj, box):
    _opt_in(jj, everything=True)
    assert _fresh(box).project_names() == ["canopy-web", "personal-site"]


def test_a_chosen_list_is_only_those_repos(jj, box):
    _opt_in(jj, projects=["canopy-web"])
    assert _fresh(box).project_names() == ["canopy-web"]


def test_a_runner_with_no_owner_opts_into_nothing(ws):
    r = Runner.objects.create(name="orphan", workspace=ws, capabilities={"projects": ["canopy-web"]})
    assert r.project_names() == []


def test_an_agents_repo_is_not_the_opt_ins_to_refuse(box):
    """An agent's own repo is checked against what the box holds, never the opt-in."""
    from apps.harness.routing import cannot_serve

    Runner.objects.filter(pk=box.pk).update(capabilities={"projects": ["hal"]})
    assert _fresh(box).project_names() == []
    assert cannot_serve(_fresh(box), "hal") == ""


# ---- mirrored sessions and transcripts -----------------------------------------


def test_a_session_in_a_repo_not_opted_in_is_never_filed(jj, ws, box):
    _opt_in(jj, projects=["canopy-web"])
    skipped: list = []
    replace_reported_sessions(_fresh(box), ws, [_reported("fix-login", "canopy-web"),
                                                _reported("blog-post", "personal-site")],
                              skipped=skipped)
    assert list(RunnerBinding.objects.values_list("session_key", flat=True)) == ["fix-login"]
    assert skipped == ["blog-post"]


def test_a_repo_leaving_the_opt_in_stops_its_transcript(jj, ws, box):
    _opt_in(jj, projects=["canopy-web"])
    replace_reported_sessions(_fresh(box), ws, [_reported("fix-login", "canopy-web")])
    streams = _client(jj).get(f"/api/harness/runners/{box.id}/streams").json()["streams"]
    assert [s["session_key"] for s in streams] == ["fix-login"]

    _opt_in(jj, projects=[])
    streams = _client(jj).get(f"/api/harness/runners/{box.id}/streams").json()["streams"]
    assert streams == []
    assert Session.objects.filter(runner_binding__session_key="fix-login").exists()  # kept, not shipped


def test_the_usage_scan_reads_only_opted_in_repos_and_keeps_no_unknown_session(jj, box):
    _opt_in(jj, projects=["canopy-web"])
    day = str(timezone.now().date())

    def rep(cs, repo):
        return {"claude_session": cs, "repo": repo, "first_at": f"{day}T09:00:00Z",
                "last_at": f"{day}T10:00:00Z", "days": [{"day": day, "input_tokens": 1}]}

    # Neither session is one canopy was sent: opted in or not, neither is kept.
    out = usage.record(_fresh(box), [rep("a", "canopy-web"), rep("b", "personal-site")])
    assert out["stored"] == 0
    assert usage.scope(_fresh(box)) == ["canopy-web"]


# ---- the account setting --------------------------------------------------------


def test_the_setting_round_trips_and_lists_what_your_boxes_hold(jj, box):
    c = _client(jj)
    body = c.get("/api/me/canopy-projects/").json()
    assert body == {"everything": False, "projects": [], "available": ["canopy-web", "personal-site"]}

    r = c.put("/api/me/canopy-projects/", {"everything": False, "projects": [" canopy-web ", "elsewhere"]},
              content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"everything": False, "projects": ["canopy-web", "elsewhere"],
                        "available": ["canopy-web", "elsewhere", "personal-site"]}
    assert _fresh(box).project_names() == ["canopy-web"]
    assert SettingChange.objects.filter(operation="set_project_opt_in").count() == 1


def test_the_setting_is_yours_alone(jj, box):
    other = User.objects.create_user("al", "al@dimagi.com", "pw")
    _client(other).put("/api/me/canopy-projects/", {"everything": True}, content_type="application/json")
    assert _fresh(box).project_names() == []
    assert ProjectOptIn.objects.get(user=other).everything is True


# ---- what the box is told to report -------------------------------------------


def _beat(user, runner):
    r = _client(user).post(f"/api/harness/runners/{runner.id}/heartbeat", {}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()["report_projects"]


def test_the_heartbeat_tells_the_box_which_repos_to_report(jj, box):
    assert _beat(jj, box) == []
    _opt_in(jj, projects=["canopy-web"])
    assert _beat(jj, box) == ["canopy-web"]
    _opt_in(jj, everything=True)
    assert _beat(jj, box) is None  # every repo


def test_an_agents_repo_on_the_box_is_always_reported(jj, ws, box):
    from apps.agents.models import Agent

    Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    Runner.objects.filter(pk=box.pk).update(capabilities={"projects": ["hal", "personal-site"]})
    assert _beat(jj, box) == ["hal"]
