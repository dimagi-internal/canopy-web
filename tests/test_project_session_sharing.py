"""Sharing your laptop sessions in a project's repo with that project's team —
`ProjectSessionShare`, leg 7 of `canopy_sessions.access.visible_session_q`.

Retired for new shares; the shares that already exist keep working:

* nobody may turn a new one on (409), so a laptop session stays its box owner's;
* a live share: the project's team (owner + participants) read, write and may
  pull; a co-tenant off the team, and any agent login, gain nothing;
* only that project's repo, only that project's workspace, never a sensitive one;
* only the person turns it off, it is audited, and off drops the next read.
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


def _existing_share(w):
    """A share made before the feature was retired."""
    return ProjectSessionShare.objects.create(user=w["lili"], project=w["project"])


def _put(w, shared, project=None):
    return _client(w["lili"]).put(f"/api/me/session-sharing/{(project or w['project']).pk}", {"shared": shared},
                                  content_type="application/json")


def test_nobody_can_start_sharing_and_nothing_is_listed_until_they_have(w):
    assert _client(w["lili"]).get("/api/me/session-sharing/").json() == []
    r = _put(w, True)
    assert r.status_code == 409
    assert not ProjectSessionShare.objects.exists()
    assert all(not access.can_read(w[k], w["in_repo"]) for k in ("lead", "teammate", "bystander"))


def test_an_existing_share_is_listed_and_still_gives_the_team_that_repo(w):
    _existing_share(w)
    listed = _client(w["lili"]).get("/api/me/session-sharing/").json()
    assert [(p["handle"], p["shared"], p["team_size"]) for p in listed] == [("supply", True, 3)]
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


def test_turning_it_off_drops_the_next_read_is_audited_and_cannot_be_undone(w):
    from apps.events.models import SettingChange

    _existing_share(w)
    assert access.can_read(w["teammate"], w["in_repo"])
    r = _put(w, False)
    assert r.status_code == 200 and r.json()["shared"] is False
    assert not access.can_read(w["teammate"], w["in_repo"])
    assert ProjectSessionShare.objects.filter(user=w["lili"], revoked_at__isnull=True).count() == 0
    assert SettingChange.objects.filter(path__contains="session-sharing").count() == 1
    assert _client(w["lili"]).get("/api/me/session-sharing/").json() == []
    assert _put(w, True).status_code == 409


def test_you_can_only_turn_off_a_share_you_have(w):
    assert _put(w, False).status_code == 404
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["lead"])
    elsewhere = AgentProject.objects.create(workspace=other, handle="x", name="X", repo_slug="connect-labs")
    ProjectSessionShare.objects.create(user=w["lead"], project=elsewhere)
    assert _put(w, False, elsewhere).status_code == 404   # someone else's share is not yours to end
    assert ProjectSessionShare.objects.filter(revoked_at__isnull=True).count() == 1
