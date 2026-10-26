"""Which agent CLI a turn may run on, and what a runner reports it has
(spec 2026-10-26-multi-cli-agents)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import clis
from apps.harness.models import Runner, RunnerLogin, Turn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()


def _ws():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    return Workspace.objects.create(slug="w", display_name="W", created_by=owner,
                                    access_request_domains=[])


def test_new_agent_defaults_to_claude_code():
    agent = Agent.objects.create(slug="a", name="A", workspace=_ws())
    agent.refresh_from_db()
    assert agent.clis == ["claude-code"]
    assert agent.cli_models == {}


def test_turn_and_session_cli_start_blank():
    ws = _ws()
    agent = Agent.objects.create(slug="a", name="A", workspace=ws)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                               idempotency_key="k")
    session = Session.objects.create(agent=agent, workspace=ws)
    assert turn.cli == ""
    assert session.cli == ""


def test_runner_login_cli_defaults_to_claude_code():
    runner = Runner.objects.create(name="r", kind=Runner.CLOUD, last_heartbeat_at=timezone.now())
    login = RunnerLogin.objects.create(runner=runner, key="default")
    assert login.cli == clis.CLAUDE_CODE == "claude-code"
    assert clis.CLIS == ("claude-code", "codex")
    assert clis.SENSITIVE_CLIS == frozenset({"claude-code"})


# ---- what a runner reports (Task 2) ----
from canopy_transcript import SIGNED_OUT_NOTE  # noqa: E402

from apps.harness import runner_protocol  # noqa: E402
from apps.harness.schemas import HeartbeatIn  # noqa: E402


def _runner(**kw):
    return Runner.objects.create(name="r", kind=Runner.CLOUD, status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), **kw)


def test_reported_legacy_box_is_claude_code_only():
    runner = _runner(capabilities={"profiles": 3, "envelope": 2})
    assert clis.reported(runner) == {"claude-code": clis.CliReport(True, 3, 2)}


def test_reported_legacy_signed_out_box():
    runner = _runner(capabilities={}, ready=False, ready_note=f"{SIGNED_OUT_NOTE} (x)")
    assert clis.reported(runner) == {"claude-code": clis.CliReport(False, 0, 0)}


def test_heartbeat_stores_sanitized_clis():
    runner = _runner(capabilities={})
    beat = {"codex": {"signed_in": True, "profiles": 3, "envelope": 2, "version": "0.164.0"},
            "gpt": {"signed_in": True}, "claude-code": "yes"}
    runner_protocol.heartbeat(runner, HeartbeatIn(clis=beat))
    runner.refresh_from_db()
    assert runner.capabilities["clis"] == {
        "codex": {"signed_in": True, "profiles": 3, "envelope": 2, "version": "0.164.0"}}
    assert clis.reported(runner) == {"codex": clis.CliReport(True, 3, 2, "0.164.0")}


def test_heartbeat_without_clis_drops_report():
    runner = _runner(capabilities={"clis": {"codex": {"signed_in": True}}})
    runner_protocol.heartbeat(runner, HeartbeatIn())
    runner.refresh_from_db()
    assert "clis" not in runner.capabilities
    assert set(clis.reported(runner)) == {"claude-code"}


def test_ws_beat_carries_clis():
    runner = _runner(capabilities={})
    frame = {"type": "heartbeat", "clis": {"codex": {"signed_in": True}}, "ready": True}
    runner_protocol.heartbeat(runner, runner_protocol.parse_heartbeat_frame(frame))
    runner.refresh_from_db()
    assert clis.reported(runner)["codex"].signed_in is True


def test_ws_beat_with_malformed_clis_keeps_the_beat():
    runner = _runner(capabilities={})
    frame = {"clis": ["codex"], "note": "still here"}
    runner_protocol.heartbeat(runner, runner_protocol.parse_heartbeat_frame(frame))
    runner.refresh_from_db()
    assert set(clis.reported(runner)) == {"claude-code"}


# ---- review fixes ----
def test_clis_is_reported_not_set_by_hand(client):
    owner = User.objects.create_user(username="own", email="own@dimagi.com")
    report = {"claude-code": {"signed_in": True, "profiles": 0, "envelope": 0, "version": ""}}
    runner = _runner(owner=owner, capabilities={"clis": report, "sessions": True})
    client.force_login(owner)
    forged = {"clis": {"claude-code": {"signed_in": True, "profiles": 99, "envelope": 99}}}
    resp = client.patch(f"/api/harness/runners/{runner.id}", data={"capabilities": forged},
                        content_type="application/json")
    assert resp.status_code == 422
    # A write that omits it (the Slack & chat toggle) keeps the box's own report.
    resp = client.patch(f"/api/harness/runners/{runner.id}",
                        data={"capabilities": {"sessions": False}}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    runner.refresh_from_db()
    assert runner.capabilities["clis"] == report
