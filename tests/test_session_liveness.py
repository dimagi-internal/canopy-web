import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.canopy_sessions import transcript_ingest
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


def _bound_session(runner=True):
    # Workspace.created_by is a required FK (NOT NULL); the rest is verbatim
    # from the brief.
    owner = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="t")
    r = None
    if runner:
        r = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    RunnerBinding.objects.create(session=s, runner=r, session_key="feat-x")
    return s, r


def test_attach_transition_sets_stream_desired_once(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    s, r = _bound_session()

    assert transcript_ingest.attach_session(s) is True   # 0 -> 1 : desired on
    assert transcript_ingest.attach_session(s) is True    # 1 -> 2 : no change
    b = RunnerBinding.objects.get(session=s)
    assert b.stream_desired is True
    # exactly one control frame on the 0->1 transition
    stream_frames = [m for _g, m in published if m.get("type") == "runner.stream"]
    assert len(stream_frames) == 1
    assert stream_frames[0]["desired"] is True
    assert stream_frames[0]["session_key"] == "feat-x"


def test_detach_to_zero_clears_stream_desired(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    s, r = _bound_session()
    transcript_ingest.attach_session(s)
    transcript_ingest.attach_session(s)
    assert transcript_ingest.detach_session(s) is True    # 2 -> 1 : still desired
    assert transcript_ingest.detach_session(s) is False   # 1 -> 0 : desired off
    assert RunnerBinding.objects.get(session=s).stream_desired is False
    assert published[-1][1]["desired"] is False


def test_attach_noop_without_binding(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    owner = User.objects.create_user("owner2", "owner2@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="web-only")
    assert transcript_ingest.attach_session(s) is False   # no binding -> nothing to stream
    assert published == []


def test_attach_rest_endpoints_tenant_gated():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    RunnerBinding.objects.create(session=s, runner=r, session_key="feat-x")
    c = Client(); c.force_login(user)
    assert c.post(f"/api/canopy-sessions/{s.id}/attach").json() == {"streaming": True}
    assert c.post(f"/api/canopy-sessions/{s.id}/detach").json() == {"streaming": False}
    # a non-member 404s
    other = User.objects.create_user("no", "no@dimagi.com", "pw")
    c2 = Client(); c2.force_login(other)
    assert c2.post(f"/api/canopy-sessions/{s.id}/attach").status_code == 404
