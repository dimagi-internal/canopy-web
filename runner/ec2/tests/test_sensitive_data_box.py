"""A sensitive-data box runs only confined turns.

docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §5.
"""
from __future__ import annotations

import json

CAP ={"name": "data", "entry": None, "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"],
       "data": "sensitive"}
TID = "3f2b8c1e-0000-4000-8000-0000000000aa"


def _turn(**kw):
    d = {"id": TID, "agent_slug": "labsdata", "prompt": "how many visits?", "mcp_token": "cct_x",
         "origin_ref": {}, "caller_context": {"profile": "confined", "capability": CAP,
                                              "conversation": {}}}
    d.update(kw)
    return d


def _wire(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    calls = {"api": []}
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: calls["api"].append((m, p, b)) or (200, {}))
    monkeypatch.setattr(mod, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: None)

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        calls["exec"] = prompt
        return True, "ok", "cli-1"

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    return calls


def _finish(calls):
    return [b for _m, p, b in calls["api"] if p.endswith("/finish")]


def test_a_sensitive_box_refuses_an_unconfined_turn(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert "exec" not in calls
    [f] = _finish(calls)
    assert f["status"] == "failed" and f["result_note"].startswith("sensitive_data_unconfined")


def test_a_confined_turn_ships_its_transcript_before_it_is_deleted(
        load_cloud_runner, monkeypatch, tmp_path):
    """The rows carry the reply to the asker (canopy-web seals them); deleting the
    transcript first left the visitor with no reply."""
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: order.append("ship"))
    monkeypatch.setattr(mod, "_sensitive_data_forget_turn", lambda *a, **k: order.append("forget"))
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert calls.get("exec"), _finish(calls)
    assert order == ["ship", "forget"]


# ── what a sensitive-data turn leaves behind, however it ends ────────────────
def _sensitive(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    mod = load_cloud_runner()
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS_HOME", tmp_path / "claude" / "projects")
    return mod


def _leave(path, text="the host's data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _own_layer(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_native_settings", lambda turn, cwd, caller: str(_leave(
        tmp_path / "profiles" / f"cloud-{TID}.settings.json", "{}")))


def test_every_session_a_failover_opened_is_deleted(load_cloud_runner, monkeypatch, tmp_path):
    """Each credential the turn tried started its own session (`--session-id`); the
    turn ends on the last, and the earlier ones' transcripts held the same words."""
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    calls = _wire(mod, monkeypatch, tmp_path)
    _own_layer(mod, monkeypatch, tmp_path)
    claude = tmp_path / "claude"
    project = claude / "projects" / mod._encode_project_dir(tmp_path)
    left = {}

    def failover(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        sids = []
        for _attempt in range(2):  # the first credential is capped; the second runs it
            cmd = mod._claude_cmd(prompt)
            sid = cmd[cmd.index("--session-id") + 1]
            sids.append(sid)
            left[sid] = [_leave(project / f"{sid}.jsonl"),
                         _leave(project / sid / "tool-results" / "out.txt"),
                         _leave(claude / "todos" / f"{sid}-agent-{sid}.json")]
        left["cmd"] = cmd
        _leave(tmp_path / "stderr.log")
        return True, "ok", sids[-1]

    monkeypatch.setattr(mod, "execute_prompt", failover)
    log = _leave(claude / "canopy" / "session-log.jsonl", '{"input_summary": {"q": "a name"}}')
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert [f["status"] for f in _finish(calls)] == ["done"]
    cmd = left.pop("cmd")
    assert len(left) == 2
    assert not any(p.exists() for paths in left.values() for p in paths)
    assert not (tmp_path / "stderr.log").exists() and not log.exists()
    assert not list((tmp_path / "caller").glob("*")) and not list((tmp_path / "profiles").glob("*"))
    # canopy-web's MCP and nothing else, for the turn's own caller token.
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == str(tmp_path / "profiles" / f"cloud-{TID}.mcp.json")


def test_a_turn_that_cannot_be_confined_still_leaves_nothing(load_cloud_runner, monkeypatch, tmp_path):
    """The caller envelope is written before confinement can fail; it still goes."""
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    calls = _wire(mod, monkeypatch, tmp_path)

    def unconfinable(turn):
        raise mod.ConfineError("could not write the session profile")

    monkeypatch.setattr(mod, "_confine", unconfinable)
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert "exec" not in calls
    assert _finish(calls)[0]["status"] == "failed"
    assert not list((tmp_path / "caller").glob("*"))


def test_a_turn_that_raises_still_leaves_nothing(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    _wire(mod, monkeypatch, tmp_path)
    _own_layer(mod, monkeypatch, tmp_path)
    project = tmp_path / "claude" / "projects" / mod._encode_project_dir(tmp_path)

    def crash(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        cmd = mod._claude_cmd(prompt)
        _leave(project / f"{cmd[cmd.index('--session-id') + 1]}.jsonl")
        raise RuntimeError("the CLI died")

    def api(method, path, body=None, **kw):
        if path.endswith("/finish"):
            raise ConnectionError("canopy-web is down")  # out to the worker's own handler
        return 200, {}

    monkeypatch.setattr(mod, "execute_prompt", crash)
    monkeypatch.setattr(mod, "_api", api)
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert not list(project.glob("*"))
    assert not list((tmp_path / "caller").glob("*")) and not list((tmp_path / "profiles").glob("*"))


def test_a_confined_turn_without_its_own_layer_is_not_run(load_cloud_runner, monkeypatch, tmp_path):
    """`_native_settings` is best-effort; on a sensitive box its absence would leave the
    turn under the whole default allow list, so the turn is refused instead."""
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    calls = _wire(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_native_settings", lambda *a: None)
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert "exec" not in calls
    [f] = _finish(calls)
    assert f["status"] == "failed" and "own permission layer" in f["result_note"]


def test_the_mcp_config_is_canopy_web_alone_as_the_caller(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "BASE_URL", "https://canopy.example.org")
    doc = json.loads(open(mod._sensitive_data_mcp_config(_turn())).read())
    assert doc == {"mcpServers": {"plugin_canopy_canopy-web": {
        "type": "http", "url": "https://canopy.example.org/api/mcp/",
        "headers": {"Authorization": "Bearer cct_x"}}}}
    # The server name keeps the plugin's tool names, so the allow list still matches.
    assert "mcp__plugin_canopy_canopy-web__*" in mod.SENSITIVE_DATA_ALLOW
    # No token of its own: a bearer canopy-web refuses, never anyone else's.
    doc = json.loads(open(mod._sensitive_data_mcp_config(_turn(mcp_token=None))).read())
    assert doc["mcpServers"]["plugin_canopy_canopy-web"]["headers"]["Authorization"] == (
        "Bearer cct_missing-confined-session-token")


def test_an_ordinary_turn_keeps_its_mcp_servers(load_cloud_runner, monkeypatch):
    monkeypatch.delenv("RUNNER_DATA_MODE", raising=False)
    cmd = load_cloud_runner()._claude_cmd("hi")
    assert "--strict-mcp-config" not in cmd and "--mcp-config" not in cmd


def test_a_restart_sweeps_what_a_dead_turn_left(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    claude = tmp_path / "claude"
    left = [_leave(tmp_path / "caller" / f"{TID}.json"),
            _leave(tmp_path / "profiles" / f"cloud-{TID}.mcp.json"),
            _leave(claude / "projects" / "-w-sessions-x" / "sid.jsonl"),
            _leave(claude / "projects" / "-w-sessions-x" / "sid" / "tool-results" / "o.txt"),
            _leave(claude / "todos" / "sid-agent-sid.json"),
            _leave(claude / "canopy" / "session-log.jsonl"),
            _leave(tmp_path / "work" / "sessions" / "x" / "stderr.log")]
    assert mod.sensitive_data_sweep_leftovers() > 0
    assert not any(p.exists() for p in left)
    assert (claude / "projects").is_dir()  # where transcripts land stays


def test_an_ordinary_box_sweeps_nothing_at_start(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.delenv("RUNNER_DATA_MODE", raising=False)
    mod = load_cloud_runner()
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path)
    kept = _leave(tmp_path / f"{TID}.json")
    assert mod.sensitive_data_sweep_leftovers() == 0 and kept.exists()


def test_the_claim_log_carries_no_prompt_on_a_sensitive_box(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    brief = mod._provenance_brief({"origin": "api", "prompt": "the patient's name is"})
    assert "prompt_head" not in brief and "patient" not in brief


def test_a_sensitive_box_ships_no_stream_or_backfill(load_cloud_runner, monkeypatch, tmp_path):
    """Only the usage report (titles blank) and slash commands leave a sensitive box."""
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: sent.append(p) or (200, {}))
    monkeypatch.setattr(mod, "_maybe_report_usage", lambda rid: sent.append("usage"))
    monkeypatch.setattr(mod, "_maybe_report_slash_commands", lambda rid: sent.append("slash"))
    mod._sync_session_views("r-1")
    mod._drain_backfills("r-1")
    mod._sync_session_streams("r-1")
    assert sent == ["usage", "slash"]


def test_a_sensitive_turn_does_not_update_the_plugin(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    assert mod.SENSITIVE_DATA_ENV["CANOPY_FLEET_AUTOUPDATE"] == "0"
    assert mod.sensitive_data_settings()["env"]["CANOPY_FLEET_AUTOUPDATE"] == "0"
