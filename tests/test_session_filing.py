"""Where a session is filed, and moving it (apps/canopy_sessions/filing.py).

Owner request (Jonathan, 2026-10-11): "I still want all my sessions that I do in
emdash to be associated with a default I can set, which is Dimagi, and then if I
belong to more than one workspace, I could move the session's association if I
want."
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import filing
from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.harness import session_sync
from apps.harness import runners
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership as M

pytestmark = pytest.mark.django_db


class _Reported:
    """Duck-types ReportedSessionIn — services reads attributes, not dict keys."""

    def __init__(self, task, project="canopy-web"):
        self.emdash_task = task
        self.project = project
        self.status = ""
        self.last_interacted_at = None
        self.recent_messages = []


def _user(name):
    return User.objects.create_user(name, f"{name}@example.org", "pw")


def _ws(slug, owner, *, parent=None):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner,
                                  parent=parent)
    M.objects.create(user=owner, workspace=ws, role=M.OWNER)
    return ws


def _join(user, ws, role=M.EDITOR):
    M.objects.update_or_create(user=user, workspace=ws, defaults={"role": role})


@pytest.fixture()
def jj():
    return _user("jj")


@pytest.fixture()
def dimagi(jj):
    return _ws("dimagi", jj)


@pytest.fixture()
def family(jj):
    return _ws("marshellis", jj)


@pytest.fixture()
def runner(jj, dimagi):
    return Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp",
                                 owner=jj, workspace=dimagi)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _report(runner, *tasks, project="canopy-web"):
    session_sync.replace_reported_sessions(
        runner, runner.workspace, [_Reported(t, project) for t in tasks], complete=True)


def _session(task):
    return Session.objects.get(runner_binding__session_key=task)


# ---- the filing rule -------------------------------------------------------


def test_unset_default_keeps_todays_behaviour(runner):
    _report(runner, "t1")
    assert _session("t1").workspace_id == "dimagi"


def test_a_new_session_is_filed_in_the_runner_owners_default(runner, jj, family):
    runners.enroll_runner(runner, "marshellis")  # a laptop serving both trees
    filing.set_default(jj, "marshellis")
    _report(runner, "t1")
    assert _session("t1").workspace_id == "marshellis"


def test_owner_default_outside_runner_tenant_is_not_used(jj, dimagi, family):
    """The default is one workspace for all of a person's boxes; a cloud box in
    the family tree must not file its sessions into dimagi, nor the reverse."""
    box = Runner.objects.create(name="cloud-ec2-2-marshellis", kind=Runner.CLOUD, host="ec2",
                                owner=jj, workspace=family)
    filing.set_default(jj, "dimagi")
    _report(box, "t1")
    assert _session("t1").workspace_id == "marshellis"


def test_session_named_after_another_trees_agent_is_not_filed_there(jj, dimagi, family):
    """`Agent.slug` is global: a family box with a checkout named `hal` is not
    reporting Dimagi's Hal, and must not file into Dimagi's tree."""
    connect = _ws("connect", jj, parent=dimagi)
    Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    box = Runner.objects.create(name="cloud-ec2-2-marshellis", kind=Runner.CLOUD, host="ec2",
                                owner=jj, workspace=family)
    resp = _client(jj).post(
        f"/api/harness/runners/{box.pk}/sessions",
        {"sessions": [{"emdash_task": "t1", "project": "hal"},
                      {"emdash_task": "t2", "project": "canopy-web"}], "complete": True},
        content_type="application/json")
    assert resp.status_code == 200, resp.content
    # Filed NOWHERE — not in Dimagi's tree (the box does not serve it), and not in
    # the family's as a plain project, where its members would read Hal's work.
    assert resp.json()["skipped"] == 1
    assert not Session.objects.filter(runner_binding__session_key="t1").exists()
    assert _session("t2").workspace_id == "marshellis"


def test_session_home_reads_the_tenant_once_per_runner(jj, dimagi, family, django_assert_num_queries):
    from apps.harness import session_sync

    box = Runner.objects.create(name="cloud-ec2-2-marshellis", kind=Runner.CLOUD, host="ec2",
                                owner=jj, workspace=family)
    session_sync.session_home(box, "canopy-web")
    # Warm: the tenant is cached on the runner, so a second session costs only
    # its agent lookup and the owner's default.
    with django_assert_num_queries(2):
        session_sync.session_home(box, "other-repo")


def test_missing_sessions_skip_another_trees_agent(jj, dimagi, family):
    from apps.events import usage
    from apps.events.models import SessionUsage

    connect = _ws("connect", jj, parent=dimagi)
    Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    box = Runner.objects.create(name="cloud-ec2-2-marshellis", kind=Runner.CLOUD, host="ec2",
                                owner=jj, workspace=family)
    for cs, repo in (("cs-hal", "hal"), ("cs-mine", "canopy-web")):
        SessionUsage.objects.create(runner=box, claude_session=cs, repo=repo, day="2026-10-18")
    rows = _client(jj).get(f"/api/harness/runners/{box.pk}/missing-sessions").json()["sessions"]
    assert [r["claude_session"] for r in rows] == ["cs-mine"]
    assert usage.send_up(box, ["cs-hal"]) == []
    assert not Session.objects.filter(runner_binding__session_key="claude:cs-hal").exists()


def test_an_agent_project_still_follows_the_agent(runner, jj, family):
    """The default is the PERSON's; an agent's project is the agent's tenant."""
    connect = _ws("connect", jj, parent=runner.workspace)
    Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    runners.enroll_runner(runner, "marshellis")
    filing.set_default(jj, "marshellis")
    _report(runner, "t1", project="hal")
    s = _session("t1")
    assert (s.workspace_id, s.agent.slug) == ("connect", "hal")


def test_a_default_the_owner_lost_is_ignored(runner, jj, family):
    """Membership is re-checked at filing time, not trusted from when it was set."""
    filing.set_default(jj, "marshellis")
    other = _user("other")
    M.objects.filter(workspace=family).update(user=other)  # jj is no longer in it
    _report(runner, "t1")
    assert _session("t1").workspace_id == "dimagi"


def test_a_default_where_the_owner_was_demoted_to_viewer_is_ignored(runner, jj, family):
    filing.set_default(jj, "marshellis")
    M.objects.filter(user=jj, workspace=family).update(role=M.VIEWER)
    _report(runner, "t1")
    assert _session("t1").workspace_id == "dimagi"


def test_existing_sessions_are_not_refiled_when_the_default_changes(runner, jj, family):
    runners.enroll_runner(runner, "marshellis")
    _report(runner, "t1")
    filing.set_default(jj, "marshellis")
    _report(runner, "t1", "t2")
    assert _session("t1").workspace_id == "dimagi"
    assert _session("t2").workspace_id == "marshellis"


def test_a_runner_with_no_owner_files_in_its_own_workspace(dimagi):
    r = Runner.objects.create(name="box", kind=Runner.EMDASH, host="box", owner=None,
                              workspace=dimagi)
    _report(r, "t1")
    assert _session("t1").workspace_id == "dimagi"


# ---- setting the default ---------------------------------------------------


def test_setting_the_default_over_the_api(jj, dimagi, family):
    c = _client(jj)
    r = c.patch("/api/me/session-workspace/", {"workspace": "dimagi"},
                content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert (body["workspace"], body["effective"]) == ("dimagi", "dimagi")
    assert {ch["slug"] for ch in body["choices"]} == {"dimagi", "marshellis"}
    assert c.get("/api/me/session-workspace/").json()["workspace"] == "dimagi"
    # Clearing.
    r = c.patch("/api/me/session-workspace/", {"workspace": None},
                content_type="application/json")
    assert r.json()["workspace"] is None


def test_the_default_must_be_a_workspace_you_belong_to(jj, dimagi):
    stranger_ws = _ws("elsewhere", _user("someone"))
    r = _client(jj).patch("/api/me/session-workspace/", {"workspace": stranger_ws.slug},
                          content_type="application/json")
    assert r.status_code == 404
    r = _client(jj).patch("/api/me/session-workspace/", {"workspace": "nope"},
                          content_type="application/json")
    assert r.status_code == 404


def test_the_default_must_be_a_workspace_you_can_edit_in(jj, dimagi):
    viewer = _user("viewer")
    _join(viewer, dimagi, M.VIEWER)
    r = _client(viewer).patch("/api/me/session-workspace/", {"workspace": "dimagi"},
                              content_type="application/json")
    assert r.status_code == 422


def test_effective_reads_null_once_the_default_no_longer_applies(jj, dimagi, family):
    filing.set_default(jj, "marshellis")
    M.objects.filter(user=jj, workspace=family).update(role=M.VIEWER)
    body = _client(jj).get("/api/me/session-workspace/").json()
    assert (body["workspace"], body["effective"]) == ("marshellis", None)


# ---- moving ----------------------------------------------------------------


def _move(user, session, slug):
    return _client(user).post(f"/api/canopy-sessions/{session.id}/move", {"workspace": slug},
                              content_type="application/json")


def test_the_runner_owner_moves_a_discovered_session(runner, jj, family):
    _report(runner, "t1")
    s = _session("t1")
    r = _move(jj, s, "marshellis")
    assert r.status_code == 200, r.content
    assert r.json()["workspace"] == "marshellis"
    s.refresh_from_db()
    assert s.workspace_id == "marshellis"


def test_detail_says_who_may_move(runner, jj, dimagi):
    _report(runner, "t1")
    s = _session("t1")
    assert _client(jj).get(f"/api/canopy-sessions/{s.id}").json()["can_move"] is True
    colleague = _user("colleague")
    _join(colleague, dimagi)
    assert _client(colleague).get(f"/api/canopy-sessions/{s.id}").json()["can_move"] is False


def test_a_re_report_does_not_move_it_back(runner, jj, family):
    """The ~10s report loop reuses the binding and must never re-file the row."""
    _report(runner, "t1")
    s = _session("t1")
    assert _move(jj, s, "marshellis").status_code == 200
    for _ in range(3):
        _report(runner, "t1")
    assert Session.objects.filter(runner_binding__session_key="t1").count() == 1
    assert _session("t1").workspace_id == "marshellis"


def test_a_re_report_after_falling_off_does_not_move_it_back_or_fork(runner, jj, family):
    _report(runner, "t1")
    s = _session("t1")
    _move(jj, s, "marshellis")
    # t1 fell off the report: the clear step nulls the binding's live runner FK.
    RunnerBinding.objects.filter(session=s).update(runner=None)
    # Still movable by its runner's owner, recovered by host.
    assert _move(jj, s, "dimagi").status_code == 200
    _move(jj, s, "marshellis")
    _report(runner, "t1")
    assert Session.objects.count() == 1
    assert _session("t1").workspace_id == "marshellis"


def test_a_colleague_cannot_move_someone_elses_runner_session(runner, jj, dimagi, family):
    _report(runner, "t1")
    colleague = _user("colleague")
    _join(colleague, dimagi)
    _join(colleague, family)
    r = _move(colleague, _session("t1"), "marshellis")
    assert r.status_code == 403
    assert _session("t1").workspace_id == "dimagi"


def test_a_web_chat_is_moved_by_its_creator_only(jj, dimagi, family):
    s = Session.objects.create(workspace=dimagi, created_by=jj, title="mine")
    colleague = _user("colleague")
    _join(colleague, dimagi)
    _join(colleague, family)
    SessionParticipant.objects.create(session=s, user=colleague, role=SessionParticipant.EDITOR)
    assert _move(colleague, s, "marshellis").status_code == 403
    assert _move(jj, s, "marshellis").status_code == 200


def test_an_agents_session_cannot_be_moved(jj, dimagi, family):
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=dimagi, owner=jj)
    s = Session.objects.create(workspace=dimagi, agent=agent, created_by=jj)
    r = _move(jj, s, "marshellis")
    assert r.status_code == 422
    assert "agent" in r.json()["detail"]
    s.refresh_from_db()
    assert s.workspace_id == "dimagi"


def test_the_target_must_be_one_you_edit_in(runner, jj, dimagi):
    _report(runner, "t1")
    s = _session("t1")
    viewer_ws = _ws("readonly", _user("boss"))
    _join(jj, viewer_ws, M.VIEWER)
    assert _move(jj, s, "readonly").status_code == 403
    assert _move(jj, s, _ws("theirs", _user("x")).slug).status_code == 404
    assert _move(jj, s, "does-not-exist").status_code == 404
    assert _session("t1").workspace_id == "dimagi"


def test_a_session_you_cannot_read_is_a_404(runner, jj, family):
    _report(runner, "t1")
    outsider = _user("outsider")
    _join(outsider, family)
    assert _move(outsider, _session("t1"), "marshellis").status_code == 404


def test_moving_is_tenant_first(jj, dimagi, family):
    """Participants outside the target lose the chat; those inside keep it; the old
    tenant's other members can no longer see a runner session."""
    _report(Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp",
                                  owner=jj, workspace=dimagi), "t1")
    s = _session("t1")
    colleague, spouse = _user("colleague"), _user("spouse")
    _join(colleague, dimagi)
    _join(spouse, dimagi)
    _join(spouse, family)
    SessionParticipant.objects.create(session=s, user=colleague, role=SessionParticipant.EDITOR)
    SessionParticipant.objects.create(session=s, user=spouse, role=SessionParticipant.VIEWER)
    assert _client(colleague).get(f"/api/canopy-sessions/{s.id}").status_code == 200

    r = _move(jj, s, "marshellis")
    assert r.status_code == 200

    assert _client(colleague).get(f"/api/canopy-sessions/{s.id}").status_code == 404
    assert set(SessionParticipant.objects.filter(session=s).values_list("user__username",
                                                                        flat=True)) == {"spouse"}
    assert _client(spouse).get(f"/api/canopy-sessions/{s.id}").status_code == 200
    assert _client(jj).get(f"/api/canopy-sessions/{s.id}").status_code == 200
    listed = {row["id"] for row in _client(colleague).get("/api/canopy-sessions/").json()}
    assert str(s.id) not in listed


def test_moving_to_where_it_already_is_is_a_no_op(runner, jj):
    _report(runner, "t1")
    s = _session("t1")
    result = filing.move(s, "dimagi", by=jj)
    assert result["moved"] is False
