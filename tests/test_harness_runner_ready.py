"""Runner readiness: the 'can I fire a turn' signal, distinct from being online."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _runner(user, ws):
    return Runner.objects.create(
        name="mbp", kind=Runner.EMDASH, host="h", paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


@pytest.fixture
def user(db):
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture
def ws(db, user):
    w = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=w, role=WorkspaceMembership.OWNER)
    return w


def test_runner_defaults_to_ready(user, ws):
    r = _runner(user, ws)
    assert r.ready is True
    assert r.ready_note == ""


def test_heartbeat_persists_not_ready_with_a_reason(user, ws):
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.post(
        f"/api/harness/runners/{r.id}/heartbeat",
        {"active_turn_ids": [], "ready": False, "ready_note": "Not logged in"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["ready"] is False
    assert body["ready_note"] == "Not logged in"
    r.refresh_from_db()
    assert r.ready is False and r.ready_note == "Not logged in"


def test_heartbeat_omitting_ready_defaults_to_ready_true(user, ws):
    """An older runner that predates the field still heartbeats — it must read as
    ready (fail OPEN: an un-upgraded runner is presumed able to fire, as today)."""
    r = _runner(user, ws)
    Runner.objects.filter(pk=r.pk).update(ready=False, ready_note="stale")
    c = Client()
    c.force_login(user)
    c.post(f"/api/harness/runners/{r.id}/heartbeat", {"active_turn_ids": []},
           content_type="application/json")
    r.refresh_from_db()
    assert r.ready is True and r.ready_note == ""


def test_heartbeat_persists_and_serves_code_branch(user, ws):
    """The runner self-reports its checkout's git branch; the supervisor uses it to
    alert on non-main (stale/wrong code). Stored + echoed in RunnerOut."""
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.post(
        f"/api/harness/runners/{r.id}/heartbeat",
        {"active_turn_ids": [], "code_branch": "ddd/external-reviewer-suggest-290"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["code_branch"] == "ddd/external-reviewer-suggest-290"
    r.refresh_from_db()
    assert r.code_branch == "ddd/external-reviewer-suggest-290"


def test_heartbeat_persists_and_serves_code_provenance(user, ws):
    """The runner self-reports the version + sha of the code it is EXECUTING, so the
    supervisor can say "that box is behind" rather than only "that checkout is on a
    branch" (spec 2026-07-28)."""
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.post(
        f"/api/harness/runners/{r.id}/heartbeat",
        {"active_turn_ids": [], "code_version": "0.2.0", "code_sha": "a" * 40},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["code_version"] == "0.2.0"
    assert resp.json()["code_sha"] == "a" * 40
    r.refresh_from_db()
    assert r.code_version == "0.2.0" and r.code_sha == "a" * 40


def test_code_provenance_is_optional_so_older_runners_keep_working(user, ws):
    """A runner that predates this — or the cloud runner, which is a different
    program entirely — sends neither field. That must stay a normal heartbeat, and
    must read as UNKNOWN (empty), never as a guess: the supervisor stays silent on
    an empty sha rather than alerting on partial information."""
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.post(f"/api/harness/runners/{r.id}/heartbeat", {"active_turn_ids": []},
                  content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["code_version"] == "" and resp.json()["code_sha"] == ""


def test_runner_out_carries_the_servers_expected_sha(user, ws, settings):
    """The client compares like with like, so the server's own expectation rides the
    row. Denormalized deliberately — see RunnerOut.expected_code_sha."""
    settings.RUNNER_CODE_SHA = "b" * 40
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.get("/api/harness/runners/")
    assert resp.status_code == 200, resp.content
    rows = [row for row in resp.json() if row["id"] == str(r.id)]
    assert rows and rows[0]["expected_code_sha"] == "b" * 40
