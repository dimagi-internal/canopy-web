"""Sensitive-data mode (`RUNNER_DATA_MODE=sensitive_data`): the ZDR API key as the one
credential, a locked profile on every turn, and nothing kept that a turn read.

docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §1. canopy counts a
person's `sensitive_data` declaration only while the box reports this mode, so what
the box does in it is the whole promise.
"""
from __future__ import annotations

import json

import pytest

from _bundles import bundle

BUNDLE = bundle("tok-1", "tok-2", api_key="sk-ant-zdr")
NO_KEY = bundle("tok-1", "tok-2")


@pytest.fixture()
def sensitive(load_cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    monkeypatch.setenv("RUNNER_EXECUTOR", "acp")
    monkeypatch.setenv("RUNNER_EGRESS", "locked")
    cr = load_cloud_runner()
    # A signed-in box login is present: sensitive-data mode must never use it.
    monkeypatch.setattr(cr, "box_login_ready", lambda *a, **k: True)
    # The lock holds (its own checks: test_egress_lock.py).
    monkeypatch.setattr(cr, "egress_probe", lambda **k: ("ok", "locked"))
    return cr


@pytest.fixture()
def ordinary(load_cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-9")
    monkeypatch.delenv("RUNNER_DATA_MODE", raising=False)
    return load_cloud_runner()


def _stage(cr, cred):
    cr._CLAUDE_CREDS.clear()
    cr._CLAUDE_CREDS.extend(cr._cascade_from(cred))


def test_the_cascade_is_the_one_zdr_key_never_a_login_or_a_subscription(sensitive):
    assert sensitive._cascade_from(BUNDLE) == [("api-key:k", "ANTHROPIC_API_KEY", "sk-ant-zdr")]
    assert sensitive._cascade_from(NO_KEY) == []
    assert sensitive._adopt_box_login() is False


def test_an_ordinary_box_keeps_its_cascade(ordinary, monkeypatch):
    monkeypatch.setattr(ordinary, "box_login_ready", lambda *a, **k: False)
    assert [c[0] for c in ordinary._cascade_from(BUNDLE)] == ["token:1", "token:2", "api-key:k"]


def test_a_sensitive_data_turn_never_bypasses_permissions(sensitive, ordinary):
    cmd = sensitive._claude_cmd("hi")
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" in ordinary._claude_cmd("hi")


def test_sensitive_data_mode_runs_claude_p_not_the_acp_adapter(sensitive):
    # ACP runs in bypassPermissions and takes no settings document.
    assert sensitive.RUNNER_EXECUTOR == "cli"


def test_the_locked_profile(sensitive):
    perms = sensitive.sensitive_data_settings()["permissions"]
    assert perms["defaultMode"] == "dontAsk"
    assert perms["disableBypassPermissionsMode"] == "disable" and perms["disableAutoMode"] == "disable"
    for tool in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "Agent", "CronCreate", "RemoteTrigger"):
        assert tool in perms["deny"]
    assert "mcp__plugin_canopy_canopy-web__*" in perms["allow"]
    assert not any(sensitive._rule_tool(a) in sensitive.SENSITIVE_DATA_DENY for a in perms["allow"])
    assert sensitive.sensitive_data_settings()["env"]["DISABLE_TELEMETRY"] == "1"


def test_a_confined_turns_rules_narrow_the_profile_and_never_widen_it(sensitive):
    confined = {"permissions": {"allow": ["mcp__plugin_canopy_canopy-web__site_call", "Bash(canopy email *)",
                                          "Read(/work/x/**)"],
                                "deny": ["Read(~/.ssh/**)"]},
                "deniedMcpServers": [{"serverName": "ace-gdrive"}]}
    doc = sensitive.sensitive_data_settings(confined)
    perms = doc["permissions"]
    assert perms["allow"] == ["mcp__plugin_canopy_canopy-web__site_call", "Read(/work/x/**)"]
    assert "Read(~/.ssh/**)" in perms["deny"] and "Bash" in perms["deny"]
    assert perms["defaultMode"] == "dontAsk"
    assert doc["deniedMcpServers"] == [{"serverName": "ace-gdrive"}]


def test_readiness_needs_the_zdr_key(sensitive):
    _stage(sensitive, NO_KEY)
    ok, note = sensitive.sensitive_data_readiness()
    assert not ok and "API keys" in note
    _stage(sensitive, BUNDLE)
    assert sensitive.sensitive_data_readiness() == (True, "")


@pytest.mark.parametrize("mode", ["zrd", "zdr"])
def test_a_mode_canopy_does_not_know_takes_no_turn(load_cloud_runner, monkeypatch, mode):
    # A typo, or the retired `zdr`: never an ordinary box, never a sensitive-data box.
    monkeypatch.setenv("RUNNER_DATA_MODE", mode)
    monkeypatch.setenv("RUNNER_EGRESS", "locked")
    cr = load_cloud_runner()
    monkeypatch.setattr(cr, "egress_probe", lambda **k: ("ok", "locked"))
    _stage(cr, BUNDLE)
    assert "--dangerously-skip-permissions" not in cr._claude_cmd("hi")
    ok, note = cr.sensitive_data_readiness()
    assert not ok and repr(mode) in note and "'sensitive_data'" in note
    cr.health_report()
    check = cr._HEALTH["claude.sensitive_data"]
    assert check["status"] == "fail" and repr(mode) in check["detail"]
    monkeypatch.setattr(cr, "health_report", lambda: {})
    body = cr._heartbeat_body([])
    assert body["ready"] is False and body["data_mode"] == mode  # which no flag accepts
    sent = []
    monkeypatch.setattr(cr, "_ws_request", lambda ws, msg, want: sent.append(msg) or {})
    assert cr._claim_and_run_once(None, "r-1") is False and sent == []
    box = {"RUNNER_NAME": "cloud-sensitive-1", "RUNNER_UNITS": "tree:dimagi", "RUNNER_DATA_MODE": mode}
    units = [{"id": "dimagi", "name": "cloud-sensitive-1", "kind": "tree", "slug": ""}]
    assert cr.egress_plan(box, units, {"cloud-sensitive-1": {"locked": False}})["dimagi"]["locked"] is True


def test_a_sensitive_data_box_reports_its_health(sensitive, ordinary):
    _stage(sensitive, BUNDLE)
    sensitive.health_report()
    assert sensitive._HEALTH["claude.sensitive_data"]["status"] == "ok"
    _stage(sensitive, {})
    sensitive.health_report()
    assert sensitive._HEALTH["claude.sensitive_data"]["status"] == "fail"
    ordinary.health_report()
    assert "claude.sensitive_data" not in ordinary._HEALTH


def test_the_heartbeat_reports_the_mode_and_a_hash(sensitive, ordinary, monkeypatch):
    _stage(sensitive, BUNDLE)
    monkeypatch.setattr(sensitive, "health_report", lambda: {})
    body = sensitive._heartbeat_body([])
    assert body["data_mode"] == "sensitive_data" and len(body["data_mode_hash"]) == 16
    assert "ready" not in body  # ready: the server default stands
    monkeypatch.setattr(ordinary, "health_report", lambda: {})
    assert ordinary._heartbeat_body([])["data_mode"] == ""


def test_a_sensitive_box_without_its_key_says_it_is_not_ready(sensitive, monkeypatch):
    _stage(sensitive, {})
    monkeypatch.setattr(sensitive, "health_report", lambda: {})
    body = sensitive._heartbeat_body([])
    assert body["ready"] is False and "API keys" in body["ready_note"]


def test_the_hash_moves_when_the_profile_does(sensitive, monkeypatch):
    _stage(sensitive, BUNDLE)
    before = sensitive.sensitive_data_posture()["data_mode_hash"]
    monkeypatch.setattr(sensitive, "SENSITIVE_DATA_ALLOW", (*sensitive.SENSITIVE_DATA_ALLOW, "WebFetch"))
    assert sensitive.sensitive_data_posture()["data_mode_hash"] != before


def test_a_turns_settings_file_merges_its_confined_layer(sensitive, tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "PROFILE_ROOT", tmp_path)
    confined = tmp_path / "cloud-t1.settings.json"
    confined.write_text(json.dumps({"permissions": {"allow": ["Read(/w/**)"], "deny": []}}))
    path = sensitive._sensitive_data_settings_file({"id": "t1"}, str(confined))
    doc = json.loads(open(path).read())
    assert doc["permissions"]["allow"] == ["Read(/w/**)"] and "Bash" in doc["permissions"]["deny"]


def test_a_confined_layer_that_cannot_be_read_stops_the_turn(sensitive, tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "PROFILE_ROOT", tmp_path)
    with pytest.raises(sensitive.ConfineError):
        sensitive._sensitive_data_settings_file({"id": "t1"}, str(tmp_path / "missing.json"))


def test_a_confined_turn_leaves_nothing_behind(sensitive, tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(sensitive, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(sensitive, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    cwd = tmp_path / "work" / "t1"
    files = [tmp_path / "caller" / "t1.json", tmp_path / "profiles" / "cloud-t1.json",
             tmp_path / "profiles" / "cloud-t1.settings.json", tmp_path / "profiles" / "cloud-t1.sensitive-data-settings.json",
             tmp_path / "projects" / sensitive._encode_project_dir(cwd) / "sid-1.jsonl"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    sensitive._sensitive_data_forget_turn("t1", cwd, "sid-1", confined=True)
    assert not any(f.exists() for f in files)


def test_a_conversations_transcript_stays_while_it_can_resume(sensitive, tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(sensitive, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(sensitive, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    cwd = tmp_path / "work" / "t1"
    transcript = tmp_path / "projects" / sensitive._encode_project_dir(cwd) / "sid-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    sensitive._sensitive_data_forget_turn("t1", cwd, "sid-1", confined=False)
    assert transcript.exists()


def test_an_ordinary_box_forgets_nothing(ordinary, tmp_path, monkeypatch):
    monkeypatch.setattr(ordinary, "CALLER_ROOT", tmp_path)
    (tmp_path / "t1.json").write_text("{}")
    ordinary._sensitive_data_forget_turn("t1", None, "", confined=True)
    assert (tmp_path / "t1.json").exists()


def test_a_sensitive_box_sends_no_transcript(sensitive, monkeypatch):
    sent = []
    monkeypatch.setattr(sensitive, "_api", lambda *a, **k: sent.append(a) or (200, {}))
    assert sensitive._post_transcript_batch("t1", "a1", 1, ['{"text": "x"}']) is False
    assert sent == []


# ── canopy-web's credential list (`cascade`) ─────────────────────────────────
LIST = {**BUNDLE, "logins": [{"key": "default"}], "cascade": [
    {"key": "k1", "kind": "api_key", "value": "sk-ant-1"},
    {"key": "default", "kind": "login", "value": ""},
    {"key": "t1", "kind": "token", "value": "tok-1"},
    {"key": "k2", "kind": "api_key", "value": "sk-ant-2"},
]}


def test_the_list_is_used_in_the_order_a_person_set(ordinary, monkeypatch):
    monkeypatch.setattr(ordinary, "box_login_ready", lambda *a, **k: True)
    monkeypatch.setattr(ordinary, "_default_adopted", lambda: True)
    assert [(c[0], c[1]) for c in ordinary._cascade_from(LIST)] == [
        ("api-key:k1", "ANTHROPIC_API_KEY"), ("box-login", "CLAUDE_CONFIG_DIR"),
        ("token:t1", "CLAUDE_CODE_OAUTH_TOKEN"), ("api-key:k2", "ANTHROPIC_API_KEY")]


def test_a_login_not_signed_in_on_the_box_is_skipped(ordinary, monkeypatch):
    monkeypatch.setattr(ordinary, "box_login_ready", lambda *a, **k: False)
    assert [c[0] for c in ordinary._cascade_from(LIST)] == ["api-key:k1", "token:t1", "api-key:k2"]


def test_stepping_onto_an_api_key_anywhere_in_the_list_notifies(ordinary, monkeypatch):
    sent = []
    monkeypatch.setattr(ordinary, "box_login_ready", lambda *a, **k: False)
    monkeypatch.setattr(ordinary, "_notify_api_key_fallback", lambda label, turn: sent.append(label))
    _stage(ordinary, LIST)
    ordinary._apply_claude_credential(0)
    assert sent == ["api-key:k1"]
    assert ordinary._advance_claude_credential() and sent == ["api-key:k1"]   # onto the token
    assert ordinary._advance_claude_credential() and sent == ["api-key:k1", "api-key:k2"]


def test_a_sensitive_box_uses_every_api_key_in_the_list_and_nothing_else(sensitive):
    assert [c[0] for c in sensitive._cascade_from(LIST)] == ["api-key:k1", "api-key:k2"]
    _stage(sensitive, LIST)
    assert sensitive.sensitive_data_readiness() == (True, "")
    _stage(sensitive, {**LIST, "cascade": [e for e in LIST["cascade"] if e["kind"] != "api_key"]})
    assert sensitive.sensitive_data_readiness()[0] is False


def test_an_api_key_first_in_the_list_notifies_once(ordinary, monkeypatch):
    """It bills from the first turn: a key a person put first, or one first
    because the login above it is not signed in."""
    sent = []
    monkeypatch.setattr(ordinary, "box_login_ready", lambda *a, **k: False)
    monkeypatch.setattr(ordinary, "_notify_api_key_fallback", lambda label, turn: sent.append(label))
    _stage(ordinary, LIST)
    ordinary._apply_claude_credential(0)
    ordinary._apply_claude_credential(0)   # a re-read re-applies the same key
    assert sent == ["api-key:k1"]


def test_a_sensitive_box_never_sends_the_billing_notice(sensitive, monkeypatch):
    sent = []
    monkeypatch.setattr(sensitive, "_notify_api_key_fallback", lambda label, turn: sent.append(label))
    _stage(sensitive, LIST)
    sensitive._apply_claude_credential(0)
    assert sensitive._advance_claude_credential() and sent == []
