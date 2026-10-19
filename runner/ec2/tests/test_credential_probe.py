"""The credential probe: "ready" means a credential just answered a prompt.

A box used to report ready the moment a value was STORED, so a capped or
signed-out token read as healthy until a real turn failed on it (cloud-ec2-fork,
2026-10-18). Each credential is now asked one short question with no tools, and
the answer is what the runner page and readiness show.
"""
from __future__ import annotations

import json
import os
import subprocess
import types

import pytest

CAP = "You've hit your weekly limit · resets 11pm (UTC)"


@pytest.fixture()
def cr(cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-fork")
    monkeypatch.setattr(cloud_runner, "PROBE_ENABLED", True)
    cloud_runner._CLAUDE_CREDS[:] = [
        ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
        ("api-key", "ANTHROPIC_API_KEY", "sk"),
    ]
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud_runner._PROBES.clear()
    return cloud_runner


def _fake_run(cr, monkeypatch, *, stdout="", stderr="", returncode=0, raises=None):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        if raises is not None:
            raise raises
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(cr.subprocess, "run", run)
    return seen


# ── the environment a probe runs in ─────────────────────────────────────────

def test_env_for_a_token_sets_only_that_var(cr):
    before = dict(os.environ)
    env = cr._credential_env(1)
    assert env["ANTHROPIC_API_KEY"] == "sk"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env0 = cr._credential_env(0)
    assert env0["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"
    assert "ANTHROPIC_API_KEY" not in env0
    # Turns run concurrently: probing must never move the process's credential.
    assert dict(os.environ) == before


def test_env_for_a_login_points_config_dir_at_it(cr, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient")
    login_dir = str(tmp_path / "logins" / "a44d94c3")
    cr._CLAUDE_CREDS[:] = [("box-login:a44d94c3", "CLAUDE_CONFIG_DIR", login_dir)]
    env = cr._credential_env(0)
    assert env["CLAUDE_CONFIG_DIR"] == login_dir
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient"


# ── classification ──────────────────────────────────────────────────────────

def test_probe_classifies_the_weekly_cap(cr, monkeypatch):
    seen = _fake_run(cr, monkeypatch, returncode=1,
                     stdout=json.dumps({"is_error": True, "result": CAP}))
    res = cr._probe_credential(0)
    assert res["label"] == "subscription-1"
    assert res["status"] == "capped"
    assert res["reset"] is not None
    assert "weekly limit" in res["detail"]
    # One short turn, no tools, never in a turn's worktree.
    assert seen["argv"][1:] == ["-p", "Reply with OK", "--output-format", "json", "--max-turns", "1"]
    assert seen["kw"]["timeout"] == cr.PROBE_TIMEOUT_SECONDS == 90
    assert seen["kw"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"


def test_probe_classifies_not_logged_in(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=1, stdout="Not logged in · Please run /login\n")
    res = cr._probe_credential(0)
    assert res["status"] == "dead"
    assert res["reset"] is None


def test_probe_ok(cr, monkeypatch):
    _fake_run(cr, monkeypatch, stdout=json.dumps({"is_error": False, "result": "OK"}))
    assert cr._probe_credential(1)["status"] == "ok"


def test_probe_timeout_is_unknown(cr, monkeypatch):
    _fake_run(cr, monkeypatch, raises=subprocess.TimeoutExpired(["claude"], 90))
    res = cr._probe_credential(0)
    assert res["status"] == "unknown"
    assert "90" in res["detail"]


def test_probe_crash_is_unknown(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=2, stdout="Segmentation fault")
    assert cr._probe_credential(0)["status"] == "unknown"


def test_probe_off_is_unknown_and_runs_nothing(cr, monkeypatch):
    monkeypatch.setattr(cr, "PROBE_ENABLED", False)
    seen = _fake_run(cr, monkeypatch, stdout="{}")
    assert cr._probe_credential(0)["status"] == "unknown"
    assert "argv" not in seen


# ── what a probe feeds ──────────────────────────────────────────────────────

def test_record_probe_sets_the_health_check(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=1,
              stdout=json.dumps({"is_error": True, "result": CAP}))
    cr._record_probe(cr._probe_credential(0))
    check = cr._HEALTH["claude.probe.subscription-1"]
    assert check["status"] == "warn"
    assert "resets" in check["detail"]
    assert cr._PROBES["subscription-1"]["status"] == "capped"


@pytest.mark.parametrize("status,check", [
    ("ok", "ok"), ("dead", "fail"), ("unknown", "warn"),
])
def test_record_probe_maps_each_state(cr, status, check):
    cr._record_probe({"label": "api-key", "status": status, "detail": "why",
                      "reset": None, "at": 0.0})
    assert cr._HEALTH["claude.probe.api-key"]["status"] == check
    if status == "dead":
        assert "not signed in" in cr._HEALTH["claude.probe.api-key"]["detail"]


def test_a_removed_credential_drops_its_probe_check(cr):
    cr._record_probe({"label": "api-key", "status": "ok", "detail": "", "reset": None, "at": 0.0})
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    cr._credential_check()
    assert "claude.probe.api-key" not in cr._HEALTH
    assert "api-key" not in cr._PROBES


# ── when probes run, readiness, and stepping aside (Task 1.2) ──────────────

def _probe(label, status, *, reset=None, at=None):
    import time as _time
    return {"label": label, "status": status, "detail": "", "reset": reset,
            "at": _time.time() if at is None else at}


def _api_log(cr, monkeypatch, replies=None):
    calls = []
    replies = replies or {}

    def api(method, path, body=None, **kw):
        calls.append((method, path, body))
        return replies.get((method, path), (200, {}))

    monkeypatch.setattr(cr, "_api", api)
    return calls


def test_staging_probes_before_the_first_ready_beat(cr, monkeypatch):
    calls = _api_log(cr, monkeypatch, {
        ("GET", "/runners/rid/credential"): (200, {"claude_token": "tok-1", "logins": []})})
    monkeypatch.setattr(cr, "_reconcile_logins", lambda *a: None)
    monkeypatch.setattr(cr, "box_login_ready", lambda **kw: False)
    monkeypatch.setattr(cr, "_probe_entry", lambda entry: _probe(entry[0], "capped"))
    assert cr.fetch_and_stage_credential("rid") is True
    assert cr._PROBES["subscription-1"]["status"] == "capped"
    assert not [c for c in calls if c[1].endswith("/heartbeat")]


def test_readiness_false_when_every_probe_fails(cr):
    import time as _time
    cr._record_probe(_probe("subscription-1", "capped", reset=_time.time() + 3600))
    cr._record_probe(_probe("api-key", "dead"))
    ready, note = cr._credential_readiness()
    assert ready is False
    assert "subscription-1" in note and "api-key" in note


def test_readiness_all_capped_names_the_earliest_reset(cr):
    import time as _time
    soon, later = _time.time() + 3600, _time.time() + 7200
    cr._record_probe(_probe("subscription-1", "capped", reset=later))
    cr._record_probe(_probe("api-key", "capped", reset=soon))
    ready, note = cr._credential_readiness()
    assert ready is False and f"earliest reset {cr._iso(soon)}" in note


def test_readiness_true_with_one_unknown(cr):
    cr._record_probe(_probe("subscription-1", "dead"))
    cr._record_probe(_probe("api-key", "unknown"))
    assert cr._credential_readiness() == (True, "")


def test_readiness_true_until_every_credential_is_probed(cr):
    cr._record_probe(_probe("subscription-1", "dead"))
    assert cr._credential_readiness()[0] is True


def test_a_cap_whose_reset_passed_counts_as_possibly_working(cr):
    import time as _time
    cr._record_probe(_probe("subscription-1", "capped", reset=_time.time() - 5))
    cr._record_probe(_probe("api-key", "dead"))
    assert cr._credential_readiness()[0] is True


def test_heartbeat_body_carries_not_ready(cr):
    assert "ready" not in cr._heartbeat_body([])     # no probes yet: the server default stands
    cr._record_probe(_probe("subscription-1", "dead"))
    cr._record_probe(_probe("api-key", "dead"))
    body = cr._heartbeat_body([])
    assert body["ready"] is False
    assert "not signed in" in body["ready_note"]
    # The credential wait's own beat still says what it says.
    assert cr._heartbeat_body([], ready=False, ready_note="waiting")["ready_note"] == "waiting"


def test_self_pause_then_self_unpause(cr, monkeypatch):
    import time as _time
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    calls = _api_log(cr, monkeypatch)
    reset = _time.time() + 3600
    cr._record_probe(_probe("subscription-1", "capped", reset=reset))
    cr._record_probe(_probe("api-key", "capped", reset=reset + 60))
    cr._sync_self_pause({"paused": False, "paused_note": ""})
    pauses = [c for c in calls if c[1] == "/runners/rid/pause"]
    assert len(pauses) == 1
    assert pauses[0][2]["note"].startswith(cr.SELF_PAUSE_PREFIX)
    assert pauses[0][2]["until"] == cr._iso(reset)
    # Already paused by itself: nothing more is posted.
    cr._sync_self_pause({"paused": True, "paused_note": pauses[0][2]["note"]})
    assert len([c for c in calls if c[1] == "/runners/rid/pause"]) == 1
    # A later probe finds a working credential: the box lifts its own pause.
    cr._record_probe(_probe("api-key", "ok"))
    cr._sync_self_pause({"paused": True, "paused_note": pauses[0][2]["note"]})
    assert ("POST", "/runners/rid/unpause", None) in calls


def test_a_dead_credential_pauses_with_no_end(cr, monkeypatch):
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    calls = _api_log(cr, monkeypatch)
    cr._record_probe(_probe("subscription-1", "dead"))
    cr._record_probe(_probe("api-key", "dead"))
    cr._sync_self_pause({"paused": False})
    body = [c for c in calls if c[1] == "/runners/rid/pause"][0][2]
    assert "until" not in body
    assert "not signed in" in body["note"]


def test_never_unpauses_a_persons_pause(cr, monkeypatch):
    """Review Focus 2: only a pause the box set itself is the box's to lift."""
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    calls = _api_log(cr, monkeypatch)
    cr._record_probe(_probe("subscription-1", "ok"))
    cr._record_probe(_probe("api-key", "ok"))
    cr._sync_self_pause({"paused": True, "paused_note": "maintenance"})
    assert not [c for c in calls if c[1].endswith("/unpause")]


def test_never_overwrites_a_persons_pause(cr, monkeypatch):
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    calls = _api_log(cr, monkeypatch)
    cr._record_probe(_probe("subscription-1", "dead"))
    cr._record_probe(_probe("api-key", "dead"))
    cr._sync_self_pause({"paused": True, "paused_note": "maintenance"})
    assert not calls


def test_the_cap_park_note_carries_the_self_pause_prefix(cr, monkeypatch):
    import time as _time
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    calls = _api_log(cr, monkeypatch)
    cr._park_until_cap_resets([_time.time() + 3600], CAP, "turn-abcdef12")
    note = [c for c in calls if c[1] == "/runners/rid/pause"][0][2]["note"]
    assert note.startswith(cr.SELF_PAUSE_PREFIX + "Claude usage cap")


def test_reread_probes_only_changed_entries(cr, monkeypatch):
    monkeypatch.setattr(cr, "_CLAUDE_CRED_RUNNER_ID", "rid")
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    cr._record_probe(_probe("subscription-1", "ok"))
    _api_log(cr, monkeypatch, {("GET", "/runners/rid/credential"): (
        200, {"claude_token": "tok-1", "claude_token_secondary": "tok-2", "logins": []})})
    monkeypatch.setattr(cr, "_reconcile_logins", lambda *a: None)
    monkeypatch.setattr(cr, "box_login_ready", lambda **kw: False)
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, **kw):
            self.target, self.args = target, args

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(cr.threading, "Thread", FakeThread)
    assert cr._reload_claude_credentials() is True
    assert started == [(["subscription-2"],)]
    # An unchanged entry keeps what it last proved.
    assert cr._PROBES["subscription-1"]["status"] == "ok"


def test_a_successful_turn_marks_its_credential_ok(cr, monkeypatch):
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (True, "done", "s"))
    monkeypatch.setattr(cr, "_promote_working_subscription", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert cr._PROBES["subscription-1"]["status"] == "ok"


def test_a_capped_turn_marks_its_credential_capped(cr, monkeypatch):
    monkeypatch.setattr(cr, "_RUNNER_ID", "")
    monkeypatch.setattr(cr, "_reload_claude_credentials", lambda: False)
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_notify_out_of_usage", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_execute_once", lambda *a, **k: (False, CAP, ""))
    cr._apply_claude_credential(0)
    cr.execute_prompt("do it", "turn-abcdef12", lambda e: None)
    assert cr._PROBES["subscription-1"]["status"] == "capped"
    assert cr._PROBES["api-key"]["status"] == "capped"


def test_probe_hang_does_not_block_serving(cr, monkeypatch):
    """Review Focus 1: a CLI that never answers gives `unknown`, which is ready."""
    _fake_run(cr, monkeypatch, raises=subprocess.TimeoutExpired(["claude"], 90))
    cr._probe_all()
    assert {p["status"] for p in cr._PROBES.values()} == {"unknown"}
    assert cr._credential_readiness() == (True, "")
    assert "ready" not in cr._heartbeat_body([])


def test_the_tick_reprobes_a_cap_once_its_reset_passes(cr, monkeypatch):
    import time as _time
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    _api_log(cr, monkeypatch)
    cr._record_probe(_probe("subscription-1", "capped", reset=_time.time() - 1,
                            at=_time.time() - 3600))
    cr._record_probe(_probe("api-key", "capped", reset=_time.time() + 3600))
    started = []
    monkeypatch.setattr(cr, "_probe_in_background", lambda labels: started.append(labels))
    cr._credential_tick({"paused": True, "paused_note": cr.SELF_PAUSE_PREFIX + "x"})
    assert started == [["subscription-1"]]


def test_a_promoted_subscription_carries_its_probe(cr, monkeypatch):
    monkeypatch.setattr(cr, "_CLAUDE_CRED_RUNNER_ID", "rid")
    _api_log(cr, monkeypatch)
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
                           ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "tok-2")]
    cr._record_probe(_probe("subscription-1", "capped"))
    cr._record_probe(_probe("subscription-2", "ok"))
    cr._apply_claude_credential(1)
    cr._promote_working_subscription("turn-abcdef12")
    # The values swapped slots, so what each value proved goes with it.
    assert cr._PROBES["subscription-1"]["status"] == "ok"
    assert cr._PROBES["subscription-2"]["status"] == "capped"
