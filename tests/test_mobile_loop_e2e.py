"""End-to-end: a dispatched Continue, claimed and executed by the REAL runner code
(drain_one), lands in the exact emdash session — with only the Electron/CDP edge
stubbed. Real HTTP (live_server), real Bearer PAT, real claim + execute_turn.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client as DjangoClient
from django.utils import timezone

# canopy_runner is a separate stdlib-only package, not on the suite's path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "canopy_runner"))
from canopy_runner import cdp_control, emdash  # noqa: E402
from canopy_runner.client import Client  # noqa: E402
from canopy_runner.config import Config  # noqa: E402
from canopy_runner.main import drain_one  # noqa: E402

from apps.canopy_sessions.models import RunnerBinding  # noqa: E402
from apps.harness.models import Runner, Turn  # noqa: E402
from apps.tokens.models import PersonalToken  # noqa: E402
from apps.workspaces.models import Workspace, WorkspaceMembership  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

HOST = "jj@test-mac"


def _seed():
    """A dimagi user (single membership → flat routing resolves), a paired runner
    that can drive canopy-web, and a raw PAT for the runner to authenticate with."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    raw, _ = PersonalToken.create_for_user(user=user, label="e2e-runner")
    runner = Runner.objects.create(
        name="test-mbp", kind=Runner.EMDASH, host=HOST, paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web"]},
    )
    return user, ws, raw, runner


def _emdash_db(tmp_path, *projects: str) -> str:
    """A real (tiny) emdash DB holding the projects this box can drive.

    The runner REPORTS its project list from this table on every healthy heartbeat
    (spec 2026-07-28), so a test that stubs emdash as healthy while leaving no DB
    behind is simulating a box that can drive nothing — and the turn it enqueues
    would correctly never be claimed. The fake has to be as real as the claim it
    makes.
    """
    path = tmp_path / "emdash.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("CREATE TABLE projects (id TEXT, name TEXT, path TEXT);")
    for i, name in enumerate(projects):
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (f"p{i}", name, f"/x/{name}"))
    conn.commit()
    conn.close()
    return str(path)


def _cfg(base_url: str, raw: str, runner_id, tmp_path) -> Config:
    return Config(
        base_url=base_url, token=raw, runner_id=str(runner_id),
        emdash_db=_emdash_db(tmp_path, "canopy-web"), state_path=str(tmp_path / "state.json"),
    )


def _record_cdp(monkeypatch):
    """Swap the Electron edge for a recorder. Returns the calls dict."""
    calls = {"open_and_send": [], "create_task": []}
    monkeypatch.setattr(
        cdp_control, "open_and_send",
        lambda task, text, **kw: (calls["open_and_send"].append((task, text)), {"ok": True})[1],
    )
    monkeypatch.setattr(
        cdp_control, "create_task",
        lambda project, prompt, **kw: (calls["create_task"].append((project, prompt)), {"task": f"{project}-new"})[1],
    )
    monkeypatch.setattr(cdp_control, "host_id", lambda: HOST)
    # A healthy emdash is exactly what the recorder simulates — say the CDP preflight
    # passes so the runner claims (instead of skipping the claim when :9222 is down,
    # which it now does to avoid burning turns).
    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **kw: True)
    # The reuse path asks sqlite "is this task live?" — say yes without a real DB.
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    return calls


def test_continue_reuses_the_exact_session_end_to_end(live_server, monkeypatch, tmp_path):
    user, ws, raw, runner = _seed()
    dc = DjangoClient()
    dc.force_login(user)

    # The runner reported this open session (creates the display row, which
    # doubles as the continue RunnerBinding — thread_key stamped by the report).
    r = dc.post(
        f"/api/harness/runners/{runner.id}/sessions",
        {"sessions": [{"emdash_task": "cloud-runner", "project": "canopy-web", "status": "in_progress"}]},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    assert RunnerBinding.objects.filter(
        session__project="canopy-web", thread_key="emdash:cloud-runner"
    ).exists()

    # The phone dispatched a Continue into that session.
    d = dc.post(
        "/api/harness/turns/",
        {"project": "canopy-web", "origin": "manual", "idempotency_key": "e2e-reuse",
         "prompt": "rerun the failing test", "origin_ref": {"thread_key": "emdash:cloud-runner"}},
        content_type="application/json",
    )
    assert d.status_code == 201, d.content
    turn_id = d.json()["id"]

    calls = _record_cdp(monkeypatch)
    result = drain_one(_cfg(live_server.url, raw, runner.id, tmp_path), Client(live_server.url, raw))

    # The prompt reached the EXACT task via reuse — not a fresh create.
    assert calls["open_and_send"] == [("cloud-runner", "rerun the failing test")]
    assert calls["create_task"] == []
    assert result.startswith("reused:")
    assert Turn.objects.get(id=turn_id).status == Turn.DONE


def test_continue_with_no_prior_session_creates_end_to_end(live_server, monkeypatch, tmp_path):
    user, ws, raw, runner = _seed()
    dc = DjangoClient()
    dc.force_login(user)

    # No report → no RunnerBinding for this thread. Dispatch a fresh Continue.
    d = dc.post(
        "/api/harness/turns/",
        {"project": "canopy-web", "origin": "manual", "idempotency_key": "e2e-create",
         "prompt": "start a fresh thread", "origin_ref": {"thread_key": "emdash:brand-new"}},
        content_type="application/json",
    )
    assert d.status_code == 201, d.content
    turn_id = d.json()["id"]

    calls = _record_cdp(monkeypatch)
    result = drain_one(_cfg(live_server.url, raw, runner.id, tmp_path), Client(live_server.url, raw))

    # A brand-new thread creates a session, not a reuse.
    assert len(calls["create_task"]) == 1
    assert calls["create_task"][0][0] == "canopy-web"
    assert "start a fresh thread" in calls["create_task"][0][1]
    assert calls["open_and_send"] == []
    assert result.startswith("created:")
    assert Turn.objects.get(id=turn_id).status == Turn.DONE
