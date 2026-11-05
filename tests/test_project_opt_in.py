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
from apps.events.models import SettingChange
from apps.harness import claim, usage
from apps.harness.models import ProjectOptIn, Runner, Turn, WorkspaceRunnerOrder
from apps.harness.session_sync import replace_reported_sessions
from apps.harness.testing import enrolled
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
    assert (body["everything"], body["projects"]) == (False, [])
    assert body["available"] == ["canopy-web", "personal-site"]

    r = c.put("/api/me/canopy-projects/", {"everything": False, "projects": [" canopy-web ", "elsewhere"]},
              content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["projects"] == ["canopy-web", "elsewhere"]
    assert r.json()["available"] == ["canopy-web", "elsewhere", "personal-site"]
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


# ---- which workspace a repo belongs to ------------------------------------------

@pytest.fixture
def family(jj):
    fam = Workspace.objects.create(slug="family", display_name="Family", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=fam, role=WorkspaceMembership.OWNER)
    return fam


@pytest.fixture
def kid(family):
    kid = User.objects.create_user("kid", "kid@example.com", "pw")
    WorkspaceMembership.objects.create(user=kid, workspace=family, role=WorkspaceMembership.EDITOR)
    return kid


@pytest.fixture
def laptop(jj, ws, family):
    """A box living in `dimagi`, enrolled in both trees and listed in family's order."""
    ProjectOptIn.objects.create(user=jj, everything=True)
    r = Runner.objects.create(
        name="laptop", kind=Runner.CLOUD, host="h", owner=jj, workspace=ws, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "projects": ["work-repo", "family-repo"]},
    )
    enrolled(r, ws, family)
    WorkspaceRunnerOrder.objects.create(workspace=family, runner=r, rank=0)
    return r


def _assign(jj, **repos):
    ProjectOptIn.objects.filter(user=jj).update(workspaces=repos)


def _project_chat_turn(ws, person, repo):
    session = Session.objects.create(workspace=ws, created_by=person, project=repo)
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API, idempotency_key=f"{ws.slug}-{repo}",
                               prompt="hi", enqueued_by=person)


def _fleet_row(person, runner):
    rows = _client(person).get("/api/harness/runners/").json()
    return next(r for r in rows if r["id"] == str(runner.pk))


def test_an_unassigned_repo_belongs_to_the_boxs_home_not_every_tree_it_serves(ws, family, laptop):
    assert laptop.offers_project_in("work-repo", ws.slug)
    assert not laptop.offers_project_in("work-repo", family.slug)


def test_an_assigned_repo_works_there_and_below_and_nowhere_else(jj, ws, family, laptop):
    division = Workspace.objects.create(slug="division", display_name="D", created_by=jj, parent=family)
    _assign(jj, **{"family-repo": ["family"]})
    laptop = Runner.objects.get(pk=laptop.pk)
    assert laptop.offers_project_in("family-repo", family.slug)
    assert laptop.offers_project_in("family-repo", division.slug)
    assert not laptop.offers_project_in("family-repo", ws.slug)


def test_a_teammates_repo_chat_cannot_reach_a_repo_not_assigned_to_their_workspace(jj, family, kid, laptop):
    turn = _project_chat_turn(family, kid, "work-repo")
    assert claim.claim_next_turn(Runner.objects.get(pk=laptop.pk)) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_a_teammates_repo_chat_runs_on_a_repo_assigned_to_their_workspace(jj, family, kid, laptop):
    _assign(jj, **{"family-repo": ["family"]})
    turn = _project_chat_turn(family, kid, "family-repo")
    claimed = claim.claim_next_turn(Runner.objects.get(pk=laptop.pk))
    assert claimed is not None and claimed.pk == turn.pk


def test_a_repo_turn_with_no_order_stays_in_the_repos_workspace(jj, family, laptop):
    WorkspaceRunnerOrder.objects.all().delete()
    Turn.objects.create(project="work-repo", workspace=family, origin=Turn.ORIGIN_API,
                        idempotency_key="repo-1", prompt="hi")
    assert claim.claim_next_turn(Runner.objects.get(pk=laptop.pk)) is None


def test_the_fleet_shows_a_member_only_their_workspaces_repos(jj, kid, laptop):
    _assign(jj, **{"family-repo": ["family"]})
    row = _fleet_row(kid, laptop)
    assert row["capabilities"]["projects"] == ["family-repo"]
    assert row["repo_workspaces"] == {"family-repo": ["family"]}


def test_the_box_admin_still_sees_every_repo_and_where_each_can_start(jj, kid, laptop):
    _assign(jj, **{"family-repo": ["family"]})
    row = _fleet_row(jj, laptop)
    assert row["capabilities"]["projects"] == ["work-repo", "family-repo"]
    assert row["repo_workspaces"] == {"work-repo": ["dimagi"], "family-repo": ["family"]}


def test_a_repo_with_no_way_to_start_is_not_offered(jj, kid, family, laptop):
    """With no order listing the box, a teammate's chat would wait forever."""
    _assign(jj, **{"family-repo": ["family"]})
    WorkspaceRunnerOrder.objects.filter(workspace=family).delete()
    row = _fleet_row(kid, laptop)
    assert row["repo_workspaces"] == {}
    assert row["capabilities"]["projects"] == ["family-repo"]


def test_a_member_still_sees_their_agents_repo_on_the_box(jj, kid, family, laptop):
    from apps.agents.models import Agent

    Agent.objects.create(slug="work-repo", name="W", workspace=family)
    assert _fleet_row(kid, laptop)["capabilities"]["projects"] == ["work-repo"]


def test_the_runners_project_list_is_narrowed_the_same_way(jj, kid, laptop):
    _assign(jj, **{"family-repo": ["family"]})
    body = _client(kid).get(f"/api/harness/runners/{laptop.pk}/projects").json()
    assert body["projects"] == ["family-repo"]


def test_the_settings_round_trip_assignments_and_audit_them(jj, ws, family, laptop):
    c = _client(jj)
    body = c.get("/api/me/canopy-projects/").json()
    assert body["default_workspaces"]["work-repo"] == ["dimagi"]
    assert {w["slug"] for w in body["workspaces"]} == {"dimagi", "family"}
    r = c.put("/api/me/canopy-projects/", {"everything": True, "repo_workspaces": {"family-repo": ["family"]}},
              content_type="application/json")
    assert r.status_code == 200
    assert r.json()["repo_workspaces"] == {"family-repo": ["family"]}
    # A write that does not name assignments leaves them alone.
    r = c.put("/api/me/canopy-projects/", {"everything": True}, content_type="application/json")
    assert r.json()["repo_workspaces"] == {"family-repo": ["family"]}
    assert SettingChange.objects.filter(operation="set_project_opt_in").exists()


def test_you_cannot_assign_a_repo_to_a_workspace_you_cannot_work_in(jj, laptop):
    stranger = User.objects.create_user("x", "x@example.com", "pw")
    Workspace.objects.create(slug="theirs", display_name="T", created_by=stranger)
    r = _client(jj).put("/api/me/canopy-projects/", {"everything": True, "repo_workspaces": {"a": ["theirs"]}},
                        content_type="application/json")
    assert r.status_code == 422


def test_a_chosen_box_is_refused_for_a_repo_not_assigned_to_the_chats_workspace(jj, family, laptop):
    from apps.canopy_sessions.placement import _placeable_runner, _placement_refused

    session = Session.objects.create(workspace=family, created_by=jj, project="work-repo")
    assert _placeable_runner(session, laptop.pk) is None
    assert "work-repo on laptop is not assigned to family" in str(_placement_refused(session, laptop.pk, "unknown"))
    _assign(jj, **{"work-repo": ["family"]})
    assert _placeable_runner(session, laptop.pk) is not None


def test_a_repo_turn_cannot_be_pinned_past_the_assignment(jj, family, laptop):
    r = _client(jj).post(f"/api/w/{family.slug}/harness/turns/",
                         {"project": "work-repo", "prompt": "hi", "runner_id": str(laptop.pk),
                          "idempotency_key": "pin-1", "origin": "api"}, content_type="application/json")
    assert r.status_code == 403, r.content
    assert "does not offer" in r.json()["detail"]
