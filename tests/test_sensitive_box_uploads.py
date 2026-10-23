"""Nothing of a sensitive-data box's transcripts reaches canopy-web except the
sealed turn-end stream: its usage keeps no title, a session canopy has no record of
is never kept or listed as missing, and nothing is sent up, backfilled or reset
from it (spec 2026-10-22-sensitive-data-agents §1, §4).

The chain this closes: a confined turn's transcript left on the box (an earlier
credential's attempt, a crash) was reported by the usage scanner, matched nothing,
was listed as missing, and `send_up` filed it as an ordinary unsealed Session and
asked the box to ship the whole transcript.
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
from apps.harness.models import Runner, RunnerFlag
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


def _report(cs, repo="canopy-web", title="the caller's first words"):
    day = str(timezone.now().date())
    return {"claude_session": cs, "repo": repo, "title": title, "first_at": f"{day}T09:00:00Z",
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


def test_an_unknown_session_on_a_sensitive_box_is_never_kept_or_listed(owner):
    box = _box(owner, sensitive=True)
    assert usage.record(box, [_report("leftover")]) == {"stored": 0, "matched": 0}
    assert not SessionUsage.objects.exists()
    assert usage.missing(box) == []
    r = _client(owner).get(f"/api/harness/runners/{box.pk}/missing-sessions")
    assert r.status_code == 200 and r.json()["sessions"] == []


def test_a_sensitive_box_keeps_no_title_even_for_a_matched_session(owner):
    box = _box(owner, sensitive=True)
    s = _bound_session(owner, box)
    RunnerBinding.objects.filter(session=s).update(transcript_id="known")
    usage.record(box, [_report("known")])
    row = SessionUsage.objects.get()
    assert (row.title, row.output_tokens) == ("", 7)


def test_an_ordinary_box_still_lists_and_titles_its_missing_sessions(owner):
    box = _box(owner, sensitive=False)
    usage.record(box, [_report("lost")])
    assert [(m["claude_session"], m["title"]) for m in usage.missing(box)] == [
        ("lost", "the caller's first words")]


def test_send_up_from_a_sensitive_box_is_refused_with_a_reason(owner):
    box = _box(owner, sensitive=False)
    usage.record(box, [_report("lost")])  # recorded before the box went sensitive
    Runner.objects.filter(pk=box.pk).update(capabilities={"data_mode": "sensitive_data"})
    r = _client(owner).post(f"/api/harness/runners/{box.pk}/missing-sessions",
                            data=json.dumps({"claude_sessions": ["lost"]}),
                            content_type="application/json")
    assert r.status_code == 409, r.content
    assert "sensitive-data" in r.json()["detail"]
    assert not Session.objects.exists()
    box.refresh_from_db()
    with pytest.raises(usage.SendUpRefusedError):
        usage.send_up(box, ["lost"])


def test_a_declared_sensitive_box_refuses_too(owner):
    """Either half counts (`is_sensitive_data_box`): the person's flag alone."""
    box = _box(owner, sensitive=False)
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=owner)
    with pytest.raises(usage.SendUpRefusedError):
        usage.send_up(box, ["anything"])


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
