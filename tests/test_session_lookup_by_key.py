"""A session can be opened by the key its Open Sessions card shows (#1127).

A cloud runner's `session_key` is a Claude session UUID, and that UUID is the
card's heading — so an agent handed "read session 1f33…" asked
`GET /api/canopy-sessions/<that uuid>`, which treated it as the primary key and
404'd. The by-id read now falls back to the key; the list filters on it too.

Neither door widens anything: both read through `access.readable_sessions`, so a
key finds exactly the sessions the caller could already open by id.
"""

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    owner = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    runner = Runner.objects.create(
        name="cloud-ec2-1", workspace=ws, location=Runner.CLOUD,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), owner=owner,
    )
    return owner, other, ws, runner


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _session(ws, runner, key, *, created_by=None, origin=Session.ORIGIN_WEB, project="p"):
    s = Session.objects.create(workspace=ws, created_by=created_by, origin=origin, title="t")
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key=key, emdash_project=project,
        last_interacted_at=timezone.now(), live_seen_at=timezone.now(),
    )
    return s


def test_a_uuid_that_is_a_session_key_opens_its_session(world):
    owner, _other, ws, runner = world
    key = str(uuid.uuid4())
    s = _session(ws, runner, key, created_by=owner)
    r = _client(owner).get(f"/api/canopy-sessions/{key}")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["id"] == str(s.id)          # the session it matched, not the key asked for
    assert body["session_key"] == key


def test_the_id_still_wins(world):
    owner, _other, ws, runner = world
    s = _session(ws, runner, str(uuid.uuid4()), created_by=owner)
    r = _client(owner).get(f"/api/canopy-sessions/{s.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(s.id)


def test_a_key_finds_nothing_the_caller_could_not_open_by_id(world):
    """The fallback reads through the one ACL: someone else's web chat stays a 404."""
    owner, other, ws, runner = world
    key = str(uuid.uuid4())
    s = _session(ws, runner, key, created_by=owner)
    assert _client(other).get(f"/api/canopy-sessions/{s.id}").status_code == 404
    r = _client(other).get(f"/api/canopy-sessions/{key}")
    assert r.status_code == 404
    # The 404 says what visibility is, so an agent stops searching and asks.
    assert "visible to you" in r.json()["detail"]


def test_a_key_naming_several_sessions_is_a_409_listing_them(world):
    owner, _other, ws, runner = world
    key = str(uuid.uuid4())
    a = _session(ws, runner, key, created_by=owner, project="p1")
    b = _session(ws, runner, key, created_by=owner, project="p2")
    r = _client(owner).get(f"/api/canopy-sessions/{key}")
    assert r.status_code == 409
    assert str(a.id) in r.json()["detail"] and str(b.id) in r.json()["detail"]


def test_the_list_filters_by_session_key(world):
    owner, other, ws, runner = world
    mine = _session(ws, runner, "canopy-web-chat-1", created_by=owner)
    _session(ws, runner, "something-else", created_by=owner, project="q")
    r = _client(owner).get("/api/canopy-sessions/?state=all&session_key=canopy-web-chat-1")
    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == [str(mine.id)]
    # Still the one ACL: the co-tenant gets nothing for the same key.
    r = _client(other).get("/api/canopy-sessions/?state=all&session_key=canopy-web-chat-1")
    assert r.json() == []
