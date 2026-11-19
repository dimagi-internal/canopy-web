"""Who coaches: `/api/coaching/workspaces/{slug}/coaches/`. An owner designates an agent or a person for the workspace and
everything below it; members may see the list (it is what a person agrees to when opting in); designating
reads nobody (only people who opted in are ever read); removing ends a coach's reach at once."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole, Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=owner)
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=ed, workspace=ws, role=WorkspaceMembership.EDITOR)
    outsider = User.objects.create_user("out", "out@example.org", "pw")
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=owner)
    foreign = Agent.objects.create(slug="foreign", name="Foreign", workspace=other, owner=owner)
    return {"owner": owner, "ed": ed, "outsider": outsider, "ws": ws, "trellis": trellis, "foreign": foreign}


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(user, body, slug="dimagi"):
    return _c(user).post(f"/api/coaching/workspaces/{slug}/coaches/", data=json.dumps(body), content_type="application/json")


def test_an_owner_designates_an_agent_and_a_person_and_a_member_can_see_who(w):
    r = _post(w["owner"], {"agent": "trellis"})
    assert r.status_code == 200 and (r.json()["kind"], r.json()["slug"]) == ("agent", "trellis")
    assert _post(w["owner"], {"agent": "trellis"}).json()["id"] == r.json()["id"]      # again: the same role
    assert _post(w["owner"], {"email": "ed@dimagi.com"}).json()["kind"] == "person"
    listing = _c(w["ed"]).get("/api/coaching/workspaces/dimagi/coaches/").json()
    assert [(c["kind"], c["name"]) for c in listing] == [("agent", "Trellis"), ("person", "ed@dimagi.com")]
    assert _c(w["outsider"]).get("/api/coaching/workspaces/dimagi/coaches/").status_code == 404


def test_only_an_owner_designates_or_removes(w):
    assert _post(w["ed"], {"agent": "trellis"}).status_code == 403
    assert _post(w["outsider"], {"agent": "trellis"}).status_code == 404
    role = _post(w["owner"], {"agent": "trellis"}).json()
    assert _c(w["ed"]).delete(f"/api/coaching/workspaces/dimagi/coaches/{role['id']}/").status_code == 403
    assert _c(w["owner"]).delete(f"/api/coaching/workspaces/dimagi/coaches/{role['id']}/").status_code == 204
    assert _c(w["owner"]).delete(f"/api/coaching/workspaces/dimagi/coaches/{role['id']}/").status_code == 404
    assert _c(w["ed"]).get("/api/coaching/workspaces/dimagi/coaches/").json() == []


def test_name_exactly_one_coach_who_belongs_to_the_tree(w):
    assert _post(w["owner"], {}).status_code == 422
    assert _post(w["owner"], {"agent": "trellis", "email": "ed@dimagi.com"}).status_code == 422
    assert _post(w["owner"], {"agent": "foreign"}).status_code == 404         # another tree's agent
    assert _post(w["owner"], {"email": "out@example.org"}).status_code == 404  # not a member
    assert _post(w["owner"], {"agent": "nobody"}).status_code == 404


def test_designating_a_coach_reads_nobody_until_a_person_opts_in_and_removing_ends_it(w):
    s = Session.objects.create(workspace=w["ws"], created_by=w["owner"], origin=Session.ORIGIN_WEB)
    role = _post(w["owner"], {"email": "ed@dimagi.com"}).json()
    assert access.can_read(w["ed"], s) is False
    CoachingOptIn.objects.create(user=w["owner"])
    assert access.can_read(w["ed"], s) is True
    _c(w["owner"]).delete(f"/api/coaching/workspaces/dimagi/coaches/{role['id']}/")
    assert access.can_read(w["ed"], s) is False
    assert CoachingRole.objects.filter(revoked_at__isnull=False).count() == 1
