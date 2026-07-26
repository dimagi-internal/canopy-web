"""Unit tests for deploy/ec2-runner/cloud_runner.py.

Everything here is pure-python or mocks subprocess.Popen / the runner's own
`_api` helper — there is no live canopy-web and no live EC2 box to test
against (see the PR2 report for what this suite does and does not cover).
"""
from __future__ import annotations

ENV_KEYS = (
    "RUNNER_SESSIONS", "RUNNER_HOST", "RUNNER_NAME", "RUNNER_PROJECTS",
    "RUNNER_AGENTS", "RUNNER_WORKSPACE",
)


def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── capability declaration ──────────────────────────────────────────────────

def test_bool_env_default_when_unset(cloud_runner):
    assert cloud_runner._bool_env("NOPE_NOT_SET", True) is True
    assert cloud_runner._bool_env("NOPE_NOT_SET", False) is False


def test_bool_env_falsy_spellings(cloud_runner, monkeypatch):
    for spelling in ("0", "false", "False", "FALSE", "no", "No", ""):
        monkeypatch.setenv("X_FLAG", spelling)
        assert cloud_runner._bool_env("X_FLAG", True) is False, spelling


def test_bool_env_truthy_spellings(cloud_runner, monkeypatch):
    for spelling in ("1", "true", "yes", "anything"):
        monkeypatch.setenv("X_FLAG", spelling)
        assert cloud_runner._bool_env("X_FLAG", False) is True, spelling


def test_sessions_capability_default_on(load_cloud_runner, monkeypatch):
    _clean_env(monkeypatch)
    mod = load_cloud_runner()
    assert mod.RUNNER_SESSIONS is True
    assert mod.RUNNER_CAPS.get("sessions") is True


def test_sessions_capability_opt_out(load_cloud_runner, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_SESSIONS", "0")
    mod = load_cloud_runner()
    assert mod.RUNNER_SESSIONS is False
    assert "sessions" not in mod.RUNNER_CAPS


def test_runner_host_defaults_to_runner_name(load_cloud_runner, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_NAME", "cloud-box-7")
    mod = load_cloud_runner()
    assert mod.RUNNER_HOST == "cloud-box-7"


def test_runner_host_explicit_override(load_cloud_runner, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_NAME", "cloud-box-7")
    monkeypatch.setenv("RUNNER_HOST", "pinned-host")
    mod = load_cloud_runner()
    assert mod.RUNNER_HOST == "pinned-host"


# ── capability sync on reuse (pair_or_load) ─────────────────────────────────

def test_pair_or_load_reuse_syncs_capabilities(cloud_runner, monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"runner_id": "existing-rid"}')
    monkeypatch.setattr(cloud_runner, "STATE_FILE", state_file)

    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/runners/existing-rid/heartbeat":
            return 200, {}
        if path == "/runners/existing-rid":
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    rid = cloud_runner.pair_or_load()

    assert rid == "existing-rid"
    methods_paths = [(m, p) for m, p, _ in calls]
    assert ("POST", "/runners/existing-rid/heartbeat") in methods_paths
    assert ("PATCH", "/runners/existing-rid") in methods_paths
    patch_body = next(b for m, p, b in calls if p == "/runners/existing-rid")
    assert patch_body == {"capabilities": cloud_runner.RUNNER_CAPS}
    # A reused runner is never re-paired via POST /runners/.
    assert all(p != "/runners/" for _m, p in methods_paths)


# ── transcript batching ──────────────────────────────────────────────────────

def test_chunk_transcript_lines_empty(cloud_runner):
    assert cloud_runner._chunk_transcript_lines([]) == []


def test_chunk_transcript_lines_stays_under_cap(cloud_runner):
    lines = [f'{{"i":{i}}}' for i in range(100)]
    batches = cloud_runner._chunk_transcript_lines(lines, max_bytes=50)
    assert [line for batch in batches for line in batch] == lines  # nothing lost, no reordering
    for batch in batches:
        assert sum(len(line.encode("utf-8")) for line in batch) <= 50 or len(batch) == 1


def test_chunk_transcript_lines_oversized_single_line_ships_alone(cloud_runner):
    huge = "x" * 1000
    batches = cloud_runner._chunk_transcript_lines(["a", huge, "b"], max_bytes=10)
    assert batches == [["a"], [huge], ["b"]]


# ── --resume argv ────────────────────────────────────────────────────────────

def test_claude_cmd_without_resume(cloud_runner):
    cmd = cloud_runner._claude_cmd("hello")
    assert "--resume" not in cmd
    assert cmd[:3] == [cloud_runner.CLAUDE_BIN, "-p", "hello"]


def test_claude_cmd_with_resume(cloud_runner):
    cmd = cloud_runner._claude_cmd("hello", "sess-123")
    assert cmd[-2:] == ["--resume", "sess-123"]


# ── session continuity plumbing ──────────────────────────────────────────────

def _session_turn(**overrides):
    turn = {
        "id": "11111111-1111-1111-1111-111111111111",
        "agent_slug": "ace",
        "project": "",
        "workspace_slug": "acme",
        "prompt": "hi",
        "origin_ref": {"thread_key": "thread-abc", "chat_session_id": "sess-uuid"},
    }
    turn.update(overrides)
    return turn


def test_session_thread_key_non_session_turn(cloud_runner):
    turn = {"agent_slug": "ace", "origin_ref": {}}
    assert cloud_runner._session_thread_key(turn) == ""


def test_session_thread_key_falls_back_to_chat_session_id(cloud_runner):
    turn = {"origin_ref": {"chat_session_id": "sess-uuid"}}
    assert cloud_runner._session_thread_key(turn) == "sess-uuid"


def test_session_resume_plan_skips_non_session_turn(cloud_runner, monkeypatch):
    called = []
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: called.append(a) or (200, {}))
    turn = {"agent_slug": "ace", "project": "", "origin_ref": {}}
    assert cloud_runner._session_resume_plan("runner-1", turn) == ""
    assert not called


def test_session_resume_plan_skips_agentless_projectless(cloud_runner, monkeypatch):
    called = []
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: called.append(a) or (200, {}))
    turn = _session_turn(agent_slug="", project="")
    assert cloud_runner._session_resume_plan("runner-1", turn) == ""
    assert not called


def test_session_resume_plan_returns_engine_handle_on_reuse(cloud_runner, monkeypatch):
    def fake_api(method, path, body=None):
        assert path == "/runners/runner-1/resolve-session"
        assert body == {"thread_key": "thread-abc", "agent_slug": "ace"}
        return 200, {"reuse": True, "emdash_task_id": "cli-sess-9", "new_thread": False}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    turn = _session_turn()
    assert cloud_runner._session_resume_plan("runner-1", turn) == "cli-sess-9"


def test_session_resume_plan_no_reuse_returns_empty(cloud_runner, monkeypatch):
    monkeypatch.setattr(
        cloud_runner, "_api",
        lambda *a, **k: (200, {"reuse": False, "emdash_task_id": "", "new_thread": True}),
    )
    turn = _session_turn()
    assert cloud_runner._session_resume_plan("runner-1", turn) == ""


def test_session_resume_plan_project_turn_sends_workspace(cloud_runner, monkeypatch):
    def fake_api(method, path, body=None):
        assert body == {"thread_key": "thread-abc", "project": "canopy-web", "workspace": "acme"}
        return 200, {"reuse": False, "emdash_task_id": ""}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    turn = _session_turn(agent_slug="", project="canopy-web")
    cloud_runner._session_resume_plan("runner-1", turn)


def test_record_session_resume_sends_both_fields(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cloud_runner, "_api",
        lambda *a, **k: calls.append(a) or (200, {}),
    )
    turn = _session_turn()
    cloud_runner._record_session_resume("runner-1", turn, "cli-sess-9")
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/runners/runner-1/record-session"
    assert body["emdash_task_id"] == "cli-sess-9"
    assert body["session_id"] == "cli-sess-9"
    assert body["thread_key"] == "thread-abc"


def test_record_session_resume_noop_without_cli_session_id(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: calls.append(a))
    cloud_runner._record_session_resume("runner-1", _session_turn(), "")
    assert not calls


def test_record_session_resume_never_raises_on_transport_failure(cloud_runner, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("network is down")

    monkeypatch.setattr(cloud_runner, "_api", boom)
    # Must not raise — a transcript/session-record hiccup must never fail the turn.
    cloud_runner._record_session_resume("runner-1", _session_turn(), "cli-sess-9")


# ── run_claude: transcript forwarding + session-id capture + resume fallback ─

class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def _stream_json_lines(session_id: str, text: str) -> list[str]:
    import json
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n",
        json.dumps({
            "type": "assistant", "session_id": session_id,
            "message": {"content": [{"type": "text", "text": text}]},
        }) + "\n",
        json.dumps({"type": "result", "session_id": session_id, "result": text, "is_error": False}) + "\n",
    ]


def test_run_claude_captures_session_id_and_forwards_transcript(cloud_runner, monkeypatch, tmp_path):
    lines = _stream_json_lines("real-cli-session-1", "hello there")
    fake_proc = _FakeProc(lines, returncode=0)
    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: fake_proc)

    transcript_calls = []

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            transcript_calls.append(body)
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)

    events = []
    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-abc12345", lambda batch: events.extend(batch), cwd=tmp_path,
    )

    assert ok is True
    assert text == "hello there"
    assert cli_session_id == "real-cli-session-1"
    # Every raw line shipped verbatim, in order, across however many batches.
    shipped = [line for call in transcript_calls for line in call["lines"]]
    assert shipped == [line.strip() for line in lines]
    # Distinct batch_ids (no accidental collisions/dedup-by-accident).
    batch_ids = [call["batch_id"] for call in transcript_calls]
    assert len(batch_ids) == len(set(batch_ids))
    assert any(e["kind"] == "assistant" for e in events)


def test_run_claude_transcript_failure_does_not_fail_turn(cloud_runner, monkeypatch, tmp_path):
    lines = _stream_json_lines("sess-2", "ok")
    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    def boom(*_a, **_k):
        raise RuntimeError("network is down")

    monkeypatch.setattr(cloud_runner, "_api", boom)
    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-xyz", lambda batch: None, cwd=tmp_path,
    )
    assert ok is True
    assert cli_session_id == "sess-2"


def test_run_claude_resume_fallback_to_fresh_spawn(cloud_runner, monkeypatch, tmp_path):
    """A --resume attempt that yields NOTHING (no lines, non-zero exit) retries
    once as a fresh spawn, dropping --resume from argv the second time."""
    fresh_lines = _stream_json_lines("brand-new-session", "fresh reply")
    popen_calls: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        if "--resume" in cmd:
            return _FakeProc([], returncode=1)  # resume target gone: nothing emitted
        return _FakeProc(fresh_lines, returncode=0)

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {}))

    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-resume", lambda batch: None, cwd=tmp_path,
        resume_session_id="stale-session-id",
    )

    assert len(popen_calls) == 2
    assert "--resume" in popen_calls[0]
    assert "--resume" not in popen_calls[1]
    assert ok is True
    assert cli_session_id == "brand-new-session"


def test_run_claude_genuine_failure_after_output_does_not_retry(cloud_runner, monkeypatch, tmp_path):
    """A resumed session that DID produce output before failing must not be
    silently retried fresh — that would duplicate work under a new session."""
    import json
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "resumed-1"}) + "\n",
        json.dumps({"type": "result", "session_id": "resumed-1", "result": "", "is_error": True}) + "\n",
    ]
    popen_calls = []

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProc(lines, returncode=1)

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {}))

    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-genuine-fail", lambda batch: None, cwd=tmp_path,
        resume_session_id="stale-session-id",
    )

    assert len(popen_calls) == 1  # no fallback retry
    assert ok is False
    assert cli_session_id == "resumed-1"
