"""Sharing your laptop sessions in a project's repo with that project's team —
the explicit multiplayer switch (`ProjectSessionShare`, leg 7 of
`canopy_sessions.access.visible_session_q`).

* a laptop session is its box owner's alone until they turn sharing on;
* on: the project's team (owner + participants) read, write and may pull;
  a co-tenant off the team, and any agent login, gain nothing;
* only that project's repo, only that project's workspace, never a sensitive one;
* only the person turns it on or off, it is audited, and off drops the next read.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, ProjectParticipant
from apps.canopy_sessions import access
from apps.canopy_sessions.models import ProjectSessionShare, RunnerBinding, Session, SessionParticipant
from apps.contacts import services as contacts
from apps.harness.models import ProjectOptIn, Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def w():
    lili, lead, teammate, bystander = (_user(n) for n in ("lili", "lead", "teammate", "bystander"))
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=lead)
    for u in (lili, lead, teammate, bystander):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=lead)
    hal.user = _user("hal-login")
    hal.save(update_fields=["user"])
    WorkspaceMembership.objects.create(user=hal.user, workspace=ws, role=WorkspaceMembership.EDITOR)
    project = AgentProject.objects.create(workspace=ws, handle="supply", name="Supply", agent=hal,
                                          repo_slug="connect-labs", owner_user=lead)
    ProjectParticipant.objects.create(project=project, person=contacts.person_for(user=teammate),
                                      source=ProjectParticipant.MANUAL)
    ProjectParticipant.objects.create(project=project, person=contacts.person_for(user=hal.user),
                                      source=ProjectParticipant.MANUAL)
    ProjectOptIn.objects.create(user=lili, projects=["connect-labs"])
    box = Runner.objects.create(name="lili-mbp", kind=Runner.EMDASH, host="lili", owner=lili,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now())

    def laptop(repo, **kw):
        s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title=repo, **kw)
        RunnerBinding.objects.create(session=s, runner=box, session_key=f"k-{s.pk.hex[:6]}",
                                     emdash_project=repo)
        return s

    return {"ws": ws, "lili": lili, "lead": lead, "teammate": teammate, "bystander": bystander,
            "hal": hal, "project": project, "in_repo": laptop("connect-labs"),
            "other_repo": laptop("canopy-web"),
            "sealed": laptop("connect-labs", metadata={"runner_requirements": ["sensitive_data"]})}


def _share(w, shared=True):
    return _client(w["lili"]).put(f"/api/me/session-sharing/{w['project'].pk}", {"shared": shared},
                                  content_type="application/json")


def test_nothing_is_shared_until_the_person_turns_it_on(w):
    listed = _client(w["lili"]).get("/api/me/session-sharing/").json()
    assert [(p["handle"], p["shared"], p["team_size"]) for p in listed] == [("supply", False, 3)]
    assert all(not access.can_read(w[k], w["in_repo"]) for k in ("lead", "teammate", "bystander"))


def test_on_the_team_reads_writes_and_pulls_only_that_repo(w):
    r = _share(w)
    assert r.status_code == 200 and r.json()["shared"] is True
    for who in ("lead", "teammate"):
        u = w[who]
        assert access.role_for(u, w["in_repo"]) == SessionParticipant.EDITOR, who
        assert access.may_pull(u, w["in_repo"]), who
        assert _client(u).get(f"/api/canopy-sessions/{w['in_repo'].pk}").status_code == 200
        assert not access.can_read(u, w["other_repo"]), who   # another repo stays hers
        assert not access.can_read(u, w["sealed"]), who       # never a sensitive one
    assert not access.can_read(w["bystander"], w["in_repo"])  # a co-tenant off the team
    assert not access.can_read(w["hal"].user, w["in_repo"])   # an agent login, even on the team
    assert access.role_for(w["lili"], w["in_repo"]) == SessionParticipant.OWNER


def test_turning_it_off_drops_the_next_read_and_both_are_audited(w):
    from apps.events.models import SettingChange

    _share(w)
    assert access.can_read(w["teammate"], w["in_repo"])
    assert _share(w, False).json()["shared"] is False
    assert not access.can_read(w["teammate"], w["in_repo"])
    assert ProjectSessionShare.objects.filter(user=w["lili"], revoked_at__isnull=True).count() == 0
    assert SettingChange.objects.filter(path__contains="session-sharing").count() == 2


def test_a_project_in_a_workspace_you_are_not_in_is_not_yours_to_share(w):
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["lead"])
    elsewhere = AgentProject.objects.create(workspace=other, handle="x", name="X", repo_slug="connect-labs")
    r = _client(w["lili"]).put(f"/api/me/session-sharing/{elsewhere.pk}", {"shared": True},
                               content_type="application/json")
    assert r.status_code == 404
    assert not ProjectSessionShare.objects.exists()
