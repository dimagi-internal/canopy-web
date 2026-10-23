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


# ── a sensitive conversation keeps its own session between turns ─────────────
# (spec 2026-10-23-sensitive-conversations-sealed-to-their-people §1)
CONV = "5e55a0b1-0000-4000-8000-00000000c0de"


def _conv_turn(tid=TID, cap=CAP, sensitive=True):
    return _turn(id=tid, sensitive_conversation=sensitive, origin_ref={"chat_session_id": CONV},
                 caller_context={"profile": "confined", "capability": cap, "conversation": {}})


def _keeping(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    calls = _wire(mod, monkeypatch, tmp_path)
    _own_layer(mod, monkeypatch, tmp_path)
    claude = tmp_path / "claude"
    project = claude / "projects" / mod._encode_project_dir(tmp_path)
    seen = calls.setdefault("seen", [])

    def run(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        # What the plugin hook would read: the envelope as the turn starts.
        env = json.loads((tmp_path / "caller" / f"{turn_id}.json").read_text())
        seen.append({"resume": resume_session_id, "conversation": env.get("conversation")})
        cmd = mod._claude_cmd(prompt, resume_session_id)
        sid = resume_session_id or cmd[cmd.index("--session-id") + 1]
        with (project / f"{sid}.jsonl").open("a") if (project / f"{sid}.jsonl").exists() else \
                _leave(project / f"{sid}.jsonl", "").open("a") as fh:
            fh.write('{"the host": "data"}\n')
        _leave(project / sid / "tool-results" / "out.txt")
        _leave(claude / "todos" / f"{sid}-agent-{sid}.json")
        _leave(claude / "file-history" / sid / "f")
        return True, "ok", sid

    monkeypatch.setattr(mod, "execute_prompt", run)
    return mod, calls, project, claude


def test_a_sensitive_conversation_resumes_its_own_session(load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, project, claude = _keeping(load_cloud_runner, monkeypatch, tmp_path)
    mod._run_turn("r-1", _conv_turn())
    [first] = calls["seen"]
    assert first["resume"] is None and first["conversation"]["starts_fresh"] is True
    sid = json.loads((tmp_path / "kept" / f"{CONV}.json").read_text())["cli_session_id"]
    # Only what --resume reads stays: the transcript and its sidecar.
    assert (project / f"{sid}.jsonl").exists() and (project / sid / "tool-results" / "out.txt").exists()
    assert not list((claude / "todos").glob("*")) and not (claude / "file-history" / sid).exists()
    # Everything else still goes at the end of every turn.
    assert not list((tmp_path / "caller").glob("*")) and not list((tmp_path / "profiles").glob("*"))

    mod._run_turn("r-1", _conv_turn(tid="3f2b8c1e-0000-4000-8000-0000000000bb"))
    second = calls["seen"][1]
    assert second["resume"] == sid and "starts_fresh" not in second["conversation"]
    assert (project / f"{sid}.jsonl").read_text().count("\n") == 2
    assert [f["status"] for f in _finish(calls)] == ["done", "done"]


def test_another_capability_starts_fresh_and_forgets_the_old_session(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, project, _claude = _keeping(load_cloud_runner, monkeypatch, tmp_path)
    mod._run_turn("r-1", _conv_turn())
    old = json.loads((tmp_path / "kept" / f"{CONV}.json").read_text())["cli_session_id"]
    mod._run_turn("r-1", _conv_turn(tid="3f2b8c1e-0000-4000-8000-0000000000bb",
                                    cap={**CAP, "name": "other"}))
    second = calls["seen"][1]
    assert second["resume"] is None and second["conversation"]["starts_fresh"] is True
    new = json.loads((tmp_path / "kept" / f"{CONV}.json").read_text())["cli_session_id"]
    assert new != old and not (project / f"{old}.jsonl").exists() and not (project / old).exists()


def test_a_conversation_canopy_does_not_call_sensitive_keeps_nothing(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, project, _claude = _keeping(load_cloud_runner, monkeypatch, tmp_path)
    mod._run_turn("r-1", _conv_turn(sensitive=False))
    mod._run_turn("r-1", _conv_turn(tid="3f2b8c1e-0000-4000-8000-0000000000bb", sensitive=False))
    assert [s["resume"] for s in calls["seen"]] == [None, None]
    assert "starts_fresh" not in calls["seen"][0]["conversation"]
    assert not list(project.glob("*")) and not (tmp_path / "kept").exists()


def test_a_resume_that_falls_back_tells_the_agent_it_starts_fresh(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, _project, _claude = _keeping(load_cloud_runner, monkeypatch, tmp_path)
    mod._run_turn("r-1", _conv_turn())
    tid = "3f2b8c1e-0000-4000-8000-0000000000bb"
    marked = []

    def fallback(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        assert resume_session_id  # planned a resume
        mod._starting_fresh()  # run_claude found it could not
        marked.append(json.loads((tmp_path / "caller" / f"{turn_id}.json").read_text()))
        return True, "ok", ""

    monkeypatch.setattr(mod, "execute_prompt", fallback)
    mod._run_turn("r-1", _conv_turn(tid=tid))
    assert marked[0]["conversation"]["starts_fresh"] is True


def _kept_conversation(mod, tmp_path, *, conv=CONV, age=0.0, now=1_000_000.0):
    cwd = tmp_path / "work" / "sessions" / conv
    project = mod.CLAUDE_PROJECTS_HOME / mod._encode_project_dir(cwd)
    files = [_leave(project / "sid-1.jsonl"), _leave(project / "sid-1" / "tool-results" / "o.txt")]
    mod._write_private(mod.KEPT_ROOT / f"{conv}.json",
                       {"cli_session_id": "sid-1", "last_used": now - age, "cwd": str(cwd),
                        "capability": "data", "agent": "labsdata"})
    return files


def test_an_idle_conversation_is_forgotten_after_a_day(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    fresh = _kept_conversation(mod, tmp_path, conv="c-fresh", age=23 * 3600)
    idle = _kept_conversation(mod, tmp_path, conv="c-idle", age=24 * 3600)
    assert mod.sweep_kept_conversations(now=1_000_000.0) == 1
    assert all(p.exists() for p in fresh) and not any(p.exists() for p in idle)
    assert not (tmp_path / "kept" / "c-idle.json").exists()


def test_a_closed_conversation_is_forgotten_and_an_unanswered_ask_closes_nothing(
        load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    still = _kept_conversation(mod, tmp_path, conv="c-open")
    closed = _kept_conversation(mod, tmp_path, conv="c-closed")
    asked = []
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: (502, None))
    assert mod.sweep_kept_conversations("r-1", now=1_000_000.0) == 0
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: asked.append((p, b)) or (
        200, {"open": ["c-open"]}))
    assert mod.sweep_kept_conversations("r-1", now=1_000_000.0) == 1
    assert asked == [("/runners/r-1/kept-conversations", {"session_ids": ["c-closed", "c-open"]})]
    assert all(p.exists() for p in still) and not any(p.exists() for p in closed)


def test_a_conversation_with_a_turn_running_is_never_swept(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    files = _kept_conversation(mod, tmp_path, age=48 * 3600)
    mod._kept_busy(CONV, 1)
    assert mod.sweep_kept_conversations(now=1_000_000.0) == 0 and all(p.exists() for p in files)
    mod._kept_busy(CONV, -1)
    assert mod.sweep_kept_conversations(now=1_000_000.0) == 1


def test_the_sweep_runs_on_its_clock(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    ran = []
    monkeypatch.setattr(mod, "sweep_kept_conversations", lambda rid, now=None: ran.append(now))
    assert mod.maybe_sweep_kept("r-1", now=10_000.0)
    assert not mod.maybe_sweep_kept("r-1", now=10_000.0 + mod.SENSITIVE_KEEP_SWEEP_SECONDS - 1)
    assert mod.maybe_sweep_kept("r-1", now=10_000.0 + mod.SENSITIVE_KEEP_SWEEP_SECONDS)
    assert len(ran) == 2


def test_a_restart_keeps_a_conversation_still_within_its_day(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    kept = _kept_conversation(mod, tmp_path)
    project = kept[0].parent
    beside = [_leave(project / "crashed-sid.jsonl"), _leave(mod._claude_home() / "todos" / "sid-1-a.json"),
              _leave(mod.CLAUDE_PROJECTS_HOME / "-elsewhere" / "x.jsonl")]
    mod.sensitive_data_sweep_leftovers()
    assert all(p.exists() for p in kept) and not any(p.exists() for p in beside)


# ── a confined turn's reply, shown as it is written ─────────────────────────
def _transcript(mod, cwd, sid):
    path = mod.CLAUDE_PROJECTS_HOME / mod._encode_project_dir(cwd) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def _say(path, role, text):
    rec = {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _posts(mod, monkeypatch):
    posts = []
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: posts.append((p, b)) or (200, {}))
    return posts


def test_a_running_confined_turn_ships_its_rows_as_they_are_written(
        load_cloud_runner, monkeypatch, tmp_path):
    """Through the turn-end ship's own route and transcript id, a few rows at a time,
    each row once — so canopy-web shows them live and the turn-end ship adds nothing."""
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    posts = _posts(mod, monkeypatch)
    path = _transcript(mod, tmp_path, "cli-1")
    tail = mod._TurnTail("r-1", "5e55-1", tmp_path)
    assert tail.tick() == 0  # no session announced yet
    tail.follow("cli-1")
    _say(path, "user", "how many visits?")
    _say(path, "assistant", "Looking now.")
    assert tail.tick() == 2
    _say(path, "assistant", "There were 12.")
    assert tail.tick() == 1
    assert tail.tick() == 0
    sent = [(p, b["transcript_id"], b["session_id"], e["kind"], e["payload"]["text"], e["index"])
            for p, b in posts for e in b["events"]]
    assert [s[:5] for s in sent] == [
        ("/runners/r-1/session-stream", "cli-1", "5e55-1", "user", "how many visits?"),
        ("/runners/r-1/session-stream", "cli-1", "5e55-1", "assistant", "Looking now."),
        ("/runners/r-1/session-stream", "cli-1", "5e55-1", "assistant", "There were 12."),
    ]
    # The ordinals the turn-end ship gives the same rows, so it is a no-op for them.
    ct = mod._transcript_core()
    whole = [r["index"] for r in ct.conversational_messages(ct.read_records(path), -1)]
    assert [s[5] for s in sent] == whole


def test_a_resumed_conversation_does_not_ship_its_earlier_turns_again(
        load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    path = _transcript(mod, tmp_path, "kept-1")
    _say(path, "user", "an earlier question")
    _say(path, "assistant", "an earlier answer")
    tail = mod._TurnTail("r-1", "5e55-1", tmp_path, resumed="kept-1")
    posts = _posts(mod, monkeypatch)
    tail.follow("kept-1")
    _say(path, "user", "and now?")
    assert tail.tick() == 1
    [(_p, body)] = posts
    assert [e["payload"]["text"] for e in body["events"]] == ["and now?"]
    assert body["events"][0]["index"] == ct_index(mod, path, 2)


def ct_index(mod, path, record):
    ct = mod._transcript_core()
    return next(r["index"] for r in ct.conversational_messages(ct.read_records(path), -1)
                if r["index"] >= ct.compose_index(record, 0))


def test_a_failed_post_is_shipped_again_next_tick(load_cloud_runner, monkeypatch, tmp_path):
    mod = _sensitive(load_cloud_runner, monkeypatch, tmp_path)
    path = _transcript(mod, tmp_path, "cli-1")
    tail = mod._TurnTail("r-1", "5e55-1", tmp_path)
    tail.follow("cli-1")
    _say(path, "assistant", "hello")
    monkeypatch.setattr(mod, "_api", lambda *a, **k: (502, {}))
    assert tail.tick() == 0
    posts = _posts(mod, monkeypatch)
    assert tail.tick() == 1 and len(posts) == 1


def test_a_confined_chat_turn_tails_while_it_runs_and_stops_before_the_ship(
        load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    order = []

    class _Tail:
        def __init__(self, runner_id, session_id, cwd, resumed=""):
            order.append(("tail", session_id))

        def follow(self, sid):
            order.append(("follow", sid))

        def start(self):
            order.append("start")

        def stop(self):
            order.append("stop")

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        mod._announce_session(turn_id, "cli-1")
        return True, "ok", "cli-1"

    monkeypatch.setattr(mod, "_TurnTail", _Tail)
    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: order.append("ship"))
    mod._run_turn("r-1", _turn(origin_ref={"chat_session_id": "5e55-1"}))
    assert _finish(calls)[0]["status"] == "done"
    assert order == [("tail", "5e55-1"), "start", ("follow", "cli-1"), "stop", "ship"]
