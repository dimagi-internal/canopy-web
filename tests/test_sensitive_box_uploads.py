"""Nothing of a sensitive-data box's transcripts reaches canopy-web except the
sealed turn-end stream: its usage carries token counts only, a session canopy has
no record of is never kept, and nothing is backfilled or reset from it (spec
2026-10-22-sensitive-data-agents §1, §4).
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import services as cs_services
from apps.canopy_sessions import transcript_read
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.events import usage
from apps.events.models import SessionUsage
from apps.harness import session_sync
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    user = User.objects.create_user("own", "own@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return user


def _box(owner, *, sensitive: bool, name="box"):
    return Runner.objects.create(
        name=name, workspace_id="w1", location=Runner.CLOUD, owner=owner, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), host=name,
        capabilities={"projects": ["canopy-web"], **({"data_mode": "sensitive_data"} if sensitive else {})})


def _report(cs, repo="canopy-web"):
    day = str(timezone.now().date())
    return {"claude_session": cs, "repo": repo, "first_at": f"{day}T09:00:00Z",
            "last_at": f"{day}T10:00:00Z",
            "days": [{"day": day, "input_tokens": 5, "output_tokens": 7, "messages": 2, "model": "m"}]}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _bound_session(owner, runner):
    s = Session.objects.create(workspace_id="w1", created_by=owner, origin=Session.ORIGIN_RUNNER,
                               project="canopy-web", title="t")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="claude:abc", transcript_id="abc")
    return s


def test_an_unknown_session_on_a_sensitive_box_is_never_kept(owner):
    box = _box(owner, sensitive=True)
    assert usage.record(box, [_report("leftover")]) == {"stored": 0, "matched": 0}
    assert not SessionUsage.objects.exists()


def test_a_sensitive_box_still_counts_a_matched_sessions_tokens(owner):
    box = _box(owner, sensitive=True)
    s = _bound_session(owner, box)
    RunnerBinding.objects.filter(session=s).update(transcript_id="known")
    usage.record(box, [_report("known")])
    assert SessionUsage.objects.get().output_tokens == 7


def test_backfill_of_a_session_bound_to_a_sensitive_box_is_refused(owner, monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    box = _box(owner, sensitive=True)
    s = _bound_session(owner, box)
    r = _client(owner).post(f"/api/canopy-sessions/{s.id}/backfill")
    assert r.status_code == 409, r.content
    assert "sensitive-data" in r.json()["detail"]
    assert not published
    assert not RunnerBinding.objects.get(session=s).backfill_requested
    with pytest.raises(transcript_read.BackfillRefusedError):
        transcript_read.request_backfill(s)


def test_a_sensitive_box_session_is_not_reset(owner, monkeypatch):
    """Reset drops the rows and asks the box for them again; the box never answers."""
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: None)
    box = _box(owner, sensitive=True)
    s = _bound_session(owner, box)
    out = cs_services.reset_session(s)
    assert (out["ok"], out["reason"]) == (False, cs_services.RESET_SENSITIVE_BOX)


def test_a_sensitive_box_is_asked_for_no_stream_or_backfill_and_cannot_push_one(owner):
    box = _box(owner, sensitive=True)
    s = _bound_session(owner, box)
    RunnerBinding.objects.filter(session=s).update(backfill_requested=True)
    c = _client(owner)
    assert c.get(f"/api/harness/runners/{box.pk}/backfills").json() == {"backfills": []}
    assert c.get(f"/api/harness/runners/{box.pk}/streams").json() == {"streams": []}
    r = c.post(f"/api/harness/runners/{box.pk}/session-backfill",
               data=json.dumps({"session_id": str(s.pk), "messages": [], "final": True,
                                "transcript_id": "abc"}),
               content_type="application/json")
    assert r.status_code == 409, r.content


def test_a_sensitive_box_files_no_new_session_from_what_it_reports(owner):
    box = _box(owner, sensitive=True)
    assert session_sync.session_home(box, "canopy-web") is None
    assert session_sync.session_home(_box(owner, sensitive=False, name="plain"), "canopy-web") is not None
