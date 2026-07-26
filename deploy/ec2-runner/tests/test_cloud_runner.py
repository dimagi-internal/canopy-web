"""Unit tests for deploy/ec2-runner/cloud_runner.py.

Everything here is pure-python or mocks subprocess.Popen / the runner's own
`_api` helper — there is no live canopy-web and no live EC2 box to test
against (see the PR2 report for what this suite does and does not cover).
"""
from __future__ import annotations

import json
import threading
import time

import pytest

# review N7: a lock-ordering regression in run_claude's threading
# (transcript_lock / transcript_post_lock) can DEADLOCK rather than raise or
# fail an assertion — verified in review by reintroducing an inverse
# acquisition order, which hung these tests until a manual --timeout stopped
# them. Without a per-test bound, that hang stalls the whole CI job instead
# of failing it. Applied to every test that drives run_claude's locking
# machinery (not just the two dedicated concurrency tests), since any of them
# could in principle hang on the same regression.
LOCK_REGRESSION_TIMEOUT = pytest.mark.timeout(20)

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


def test_sessions_capability_default_off(load_cloud_runner, monkeypatch):
    """Opt-in, not opt-out: this runner has no durable-record path for a chat
    session yet (review C1), so claiming session turns must not happen unless
    an operator deliberately turns it on."""
    _clean_env(monkeypatch)
    mod = load_cloud_runner()
    assert mod.RUNNER_SESSIONS is False
    assert "sessions" not in mod.RUNNER_CAPS


def test_sessions_capability_opt_in(load_cloud_runner, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_SESSIONS", "1")
    mod = load_cloud_runner()
    assert mod.RUNNER_SESSIONS is True
    assert mod.RUNNER_CAPS.get("sessions") is True


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


def test_chunk_transcript_lines_oversized_single_line_becomes_marker(cloud_runner):
    """An oversized line can never fit even alone (the server 422s the whole
    request over its byte cap before the 100MB per-turn ceiling is ever
    consulted) — it must not be shipped verbatim, and must not vanish either
    (review I4)."""
    huge = "x" * 1000
    batches = cloud_runner._chunk_transcript_lines(["a", huge, "b"], max_bytes=10)
    assert batches[0] == ["a"]
    assert batches[-1] == ["b"]
    assert len(batches) == 3
    marker = json.loads(batches[1][0])
    assert marker["type"] == "canopy_runner_line_dropped"
    assert marker["bytes"] == 1000
    # The marker itself must fit comfortably under the cap, or it would just
    # recreate the same problem.
    assert len(batches[1][0].encode("utf-8")) <= 200


def test_chunk_transcript_lines_oversized_line_does_not_merge_with_neighbors(cloud_runner):
    huge = "y" * 500
    batches = cloud_runner._chunk_transcript_lines(["a", "b", huge, "c", "d"], max_bytes=10)
    # "a","b" batch together; the marker stands alone; "c","d" batch together.
    assert batches[0] == ["a", "b"]
    assert json.loads(batches[1][0])["type"] == "canopy_runner_line_dropped"
    assert batches[2] == ["c", "d"]


# ── --resume argv ────────────────────────────────────────────────────────────

def test_claude_cmd_without_resume(cloud_runner):
    cmd = cloud_runner._claude_cmd("hello")
    assert "--resume" not in cmd
    assert cmd[:3] == [cloud_runner.CLAUDE_BIN, "-p", "hello"]


def test_claude_cmd_with_resume(cloud_runner):
    cmd = cloud_runner._claude_cmd("hello", "sess-123")
    assert cmd[-2:] == ["--resume", "sess-123"]


# ── resume-target existence check (review C2 / I1) ──────────────────────────

def _stage_resume_target(cloud_runner, monkeypatch, tmp_path, cwd, session_id):
    """Make it look like claude has a real transcript for (cwd, session_id) —
    the exact convention `_resume_target_exists` reads
    (~/.claude/projects/<encoded-cwd>/<session-id>.jsonl)."""
    projects_home = tmp_path / "claude-projects-home"
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", projects_home)
    proj_dir = projects_home / cloud_runner._encode_project_dir(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{session_id}.jsonl").write_text("")
    return projects_home


def test_resume_target_exists_false_when_no_file(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "nope")
    assert cloud_runner._resume_target_exists(tmp_path / "work", "some-session") is False


def test_resume_target_exists_true_when_staged(cloud_runner, monkeypatch, tmp_path):
    cwd = tmp_path / "work"
    _stage_resume_target(cloud_runner, monkeypatch, tmp_path, cwd, "some-session")
    assert cloud_runner._resume_target_exists(cwd, "some-session") is True


def test_resume_target_exists_false_for_empty_session_id(cloud_runner, tmp_path):
    assert cloud_runner._resume_target_exists(tmp_path, "") is False


# ── stable per-session workdir (review C2) ──────────────────────────────────

def test_turn_cwd_session_turn_is_stable_across_turn_ids(cloud_runner):
    """The whole point of the fix: two DIFFERENT turns on the SAME canopy
    Session must resolve to the SAME cwd, or --resume can never find its
    target (Claude Code resolves a session by cwd-derived project dir)."""
    turn_a = {"origin_ref": {"chat_session_id": "sess-fixed-1"}}
    turn_b = {"origin_ref": {"chat_session_id": "sess-fixed-1"}}
    cwd_a = cloud_runner._turn_cwd(turn_a, "turn-id-aaaaaaaa")
    cwd_b = cloud_runner._turn_cwd(turn_b, "turn-id-bbbbbbbb")
    assert cwd_a == cwd_b


def test_turn_cwd_different_sessions_get_different_dirs(cloud_runner):
    turn_a = {"origin_ref": {"chat_session_id": "sess-1"}}
    turn_b = {"origin_ref": {"chat_session_id": "sess-2"}}
    assert (cloud_runner._turn_cwd(turn_a, "t1")
            != cloud_runner._turn_cwd(turn_b, "t2"))


def test_turn_cwd_session_turn_ignores_agent_slug(cloud_runner):
    """A session turn can carry agent_slug too (you chat WITH an agent), but it
    must still get the stable session dir, not the agent's clone — a session
    turn is one message in an ongoing conversation, not "go work in this repo"."""
    turn = {"agent_slug": "ace", "origin_ref": {"chat_session_id": "sess-99"}}
    cwd = cloud_runner._turn_cwd(turn, "t1")
    assert "sessions" in cwd.parts
    assert "sess-99" in cwd.parts


def test_turn_cwd_non_session_agent_turn_unaffected(cloud_runner, tmp_path, monkeypatch):
    """A non-session agent turn with no bootstrapped clone (no AGENT_ROOT/<slug>/
    .git) keeps the pre-existing turn-id-keyed scratch-dir behavior — the C2
    fix only changes behavior for SESSION turns."""
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))  # no .git -> falls through
    turn = {"agent_slug": "ace", "origin_ref": {}}
    cwd = cloud_runner._turn_cwd(turn, "turn-id-12345678")
    assert cwd == cloud_runner.pathlib.Path(cloud_runner.WORK_DIR) / "turn-id-12345678"[:8]
    assert "sessions" not in cwd.parts


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
    called = []

    def fake_api(method, path, body=None):
        called.append((method, path, body))
        assert body == {"thread_key": "thread-abc", "project": "canopy-web", "workspace": "acme"}
        return 200, {"reuse": False, "emdash_task_id": ""}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    turn = _session_turn(agent_slug="", project="canopy-web")
    cloud_runner._session_resume_plan("runner-1", turn)
    # Vacuous-pass guard (review): if _session_resume_plan stopped calling _api
    # entirely, the assertions inside fake_api would never run and this test
    # would pass for the wrong reason.
    assert called, "_session_resume_plan never called _api"


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


# ── transcript POST retry / truncation (review I5) ──────────────────────────

def test_post_transcript_batch_retries_then_succeeds(cloud_runner, monkeypatch):
    attempts = []

    def flaky_api(method, path, body=None):
        attempts.append(body["batch_id"])
        if len(attempts) < 3:
            return 500, None
        return 200, {"truncated": False}

    monkeypatch.setattr(cloud_runner, "_api", flaky_api)
    monkeypatch.setattr(cloud_runner.time, "sleep", lambda *_a: None)  # don't slow the test down
    ok = cloud_runner._post_transcript_batch("turn-1", "att1", 1, ["line-a"])
    assert ok is True
    assert len(attempts) == 3
    # Same batch_id on every retry — a fabricated new id per attempt would
    # defeat the server's dedup and is exactly what the docstring warns against.
    assert len(set(attempts)) == 1


def test_post_transcript_batch_gives_up_after_max_retries_without_raising(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (500, None))
    monkeypatch.setattr(cloud_runner.time, "sleep", lambda *_a: None)
    # Must not raise, and must return True (keep trying future batches) even
    # though THIS batch never landed.
    assert cloud_runner._post_transcript_batch("turn-1", "att1", 1, ["line-a"]) is True


def test_post_transcript_batch_stops_on_truncated(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {"truncated": True}))
    assert cloud_runner._post_transcript_batch("turn-1", "att1", 1, ["line-a"]) is False


# ── run_claude: transcript forwarding + session-id capture + resume fallback ─

class _FakeProc:
    def __init__(self, lines, returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        # Accepting (and ignoring) `timeout` matters: without it, EVERY test
        # using this fake would hit a TypeError on run_claude's real
        # `proc.wait(timeout=...)` call, silently masked by the `finally`
        # block's broad `except Exception`, and would never actually
        # exercise the real reap path (review N4's regression lived exactly
        # in that path).
        return self.returncode

    def kill(self):
        self.killed = True


def _stream_json_lines(session_id: str, text: str) -> list[str]:
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
    # batch_id is `{turn_id}:{attempt_id}:{seq}` — STABLE attempt id across
    # every batch of this one attempt, with strictly increasing seq. A bare
    # uuid4() per POST (the exact regression this guards against — it would
    # destroy the server's retry-dedup) would fail the "same attempt id"
    # assertion below even though a naive distinctness check could not
    # tell it apart from the real thing.
    parsed = [call["batch_id"].split(":") for call in transcript_calls]
    assert all(p[0] == "turn-abc12345" for p in parsed)
    attempt_ids = {p[1] for p in parsed}
    assert len(attempt_ids) == 1, "attempt id must be stable across all batches of one attempt"
    seqs = [int(p[2]) for p in parsed]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
    assert any(e["kind"] == "assistant" for e in events)
    # review N4: a clean turn that reached EOF on its own must never be
    # killed — only a genuinely hung child (bounded wait times out) may be.
    assert fake_proc.killed is False


def test_run_claude_clean_turn_without_result_event_is_not_killed_or_marked_failed(
    cloud_runner, monkeypatch, tmp_path,
):
    """review N4: the regression's exact precondition — claude ends its
    stdout stream (no more stream-json lines, i.e. the loop reaches EOF)
    WITHOUT ever emitting a `result` event, then exits 0 (e.g. real `claude`
    doing a little housekeeping — closing out its own transcript file, the
    very file I1/C2's --resume depends on — after its last visible line).
    An unconditional kill()-before-wait() at EOF would SIGKILL a process
    that was already exiting cleanly, flipping this turn's outcome to a
    stderr-tail failure even though nothing went wrong. Must stay ok=True,
    and kill() must never be called for this shape."""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-clean"}) + "\n",
        json.dumps({
            "type": "assistant", "session_id": "sess-clean",
            "message": {"content": [{"type": "text", "text": "all done"}]},
        }) + "\n",
        # No `result` line — the loop still reaches EOF (proc.stdout is
        # exhausted) and the process is about to exit 0 on its own.
    ]
    fake_proc = _FakeProc(lines, returncode=0)
    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {}))

    ok, _text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-clean-no-result", lambda batch: None, cwd=tmp_path,
    )

    assert fake_proc.killed is False
    assert ok is True
    assert cli_session_id == "sess-clean"


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


def test_run_claude_no_resume_target_never_invokes_resume(cloud_runner, monkeypatch, tmp_path):
    """No staged transcript for the given (cwd, session_id) -> claude is never
    even invoked with --resume; it goes straight to a fresh spawn in ONE
    subprocess call, not a wasted resume-then-fallback round trip."""
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "nothing-here")
    fresh_lines = _stream_json_lines("brand-new", "fresh")
    popen_calls = []

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProc(fresh_lines, returncode=0)

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (200, {}))

    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-noresume", lambda batch: None, cwd=tmp_path / "work",
        resume_session_id="never-existed",
    )
    assert len(popen_calls) == 1
    assert "--resume" not in popen_calls[0]
    assert ok is True
    assert cli_session_id == "brand-new"


def test_run_claude_resume_fallback_to_fresh_spawn(cloud_runner, monkeypatch, tmp_path):
    """A staged (existing) --resume target that STILL yields NOTHING (no
    lines, non-zero exit) retries once as a fresh spawn, dropping --resume
    from argv the second time."""
    cwd = tmp_path / "work"
    _stage_resume_target(cloud_runner, monkeypatch, tmp_path, cwd, "stale-session-id")
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
        "hi", "turn-resume", lambda batch: None, cwd=cwd,
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
    cwd = tmp_path / "work"
    _stage_resume_target(cloud_runner, monkeypatch, tmp_path, cwd, "stale-session-id")
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
        "hi", "turn-genuine-fail", lambda batch: None, cwd=cwd,
        resume_session_id="stale-session-id",
    )

    assert len(popen_calls) == 1  # no fallback retry
    assert ok is False
    assert cli_session_id == "resumed-1"


@LOCK_REGRESSION_TIMEOUT
def test_run_claude_production_batching_crosses_flush_threshold(cloud_runner, monkeypatch, tmp_path):
    """Drives run_claude's OWN byte-threshold flush logic (not just the pure
    _chunk_transcript_lines helper) — review coverage gap: feed enough bytes
    to cross TRANSCRIPT_FLUSH_BYTES twice mid-stream and assert more than one
    transcript POST happened, with the full content preserved across them."""
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_BYTES", 200)
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_SECONDS", 999)  # isolate the byte trigger
    big_text = "z" * 100
    lines = []
    for i in range(6):
        lines.append(json.dumps({
            "type": "assistant", "session_id": "sess-batch",
            "message": {"content": [{"type": "text", "text": f"{big_text}-{i}"}]},
        }) + "\n")
    lines.append(json.dumps({"type": "result", "session_id": "sess-batch", "result": "done", "is_error": False}) + "\n")

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    transcript_calls = []

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            transcript_calls.append(body)
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    ok, _text, _sid = cloud_runner.run_claude(
        "hi", "turn-batching", lambda batch: None, cwd=tmp_path,
    )
    assert ok is True
    assert len(transcript_calls) > 1, "expected the byte threshold to trigger more than one flush"
    shipped = [line for call in transcript_calls for line in call["lines"]]
    assert shipped == [line.strip() for line in lines]


@LOCK_REGRESSION_TIMEOUT
def test_run_claude_periodic_flush_fires_during_a_quiet_stdout(cloud_runner, monkeypatch, tmp_path):
    """review I2: the byte/size flush only runs when a NEW line arrives, so a
    long-quiet stdout (a multi-minute tool call) must not sit on unflushed
    lines until the process exits. Uses a short TRANSCRIPT_FLUSH_SECONDS and a
    slow-yielding fake stdout so the background flush thread gets a real
    chance to fire mid-turn."""
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_SECONDS", 0.05)
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_BYTES", 10 ** 9)  # isolate the timer trigger

    first_line = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-slow"}) + "\n"
    second_line = json.dumps(
        {"type": "result", "session_id": "sess-slow", "result": "done", "is_error": False}
    ) + "\n"

    class _SlowFakeProc:
        def __init__(self):
            self.returncode = 0

        @property
        def stdout(self):
            yield first_line
            time.sleep(0.3)  # generous vs. the 0.05s flush interval above
            yield second_line

        def wait(self):
            return self.returncode

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: _SlowFakeProc())
    transcript_calls = []
    lock = threading.Lock()

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            with lock:
                transcript_calls.append(body)
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    ok, _text, _sid = cloud_runner.run_claude(
        "hi", "turn-slow", lambda batch: None, cwd=tmp_path,
    )
    assert ok is True
    # The first line must have been flushed by the PERIODIC thread while the
    # loop was still blocked waiting for the second line — i.e. more than one
    # POST happened, not just the single final flush.
    assert len(transcript_calls) >= 2


class _HangingFakeProc:
    """Simulates claude STILL ALIVE and blocked on a full, undrained stdout
    pipe at the moment the read loop exits early via an exception (review
    N1's exact scenario) — `_FakeProc`'s plain list-iterator stdout cannot
    represent this, because iterating it to exhaustion is what "the loop
    reached EOF" means; this fake is deliberately never exhausted.

    Rather than literally hanging (which would hang the test suite on a
    regression), `wait()` FAILS THE TEST LOUDLY if called before the process
    was killed/its stdout closed, or without a bounded timeout — the two
    preconditions the fix must establish. That makes this test deterministic
    AND diagnostic: run against the pre-fix code (bare `proc.wait()`, no
    kill, no timeout), it fails immediately with an assertion naming exactly
    what's missing, instead of hanging pytest.
    """

    def __init__(self, lines):
        self._iter = iter(lines)
        self.returncode = None
        self.killed = False
        self.stdout_closed = False

    @property
    def stdout(self):
        return self  # so both `for line in proc.stdout` and `proc.stdout.close()` work

    def __iter__(self):
        return self._iter

    def close(self):
        self.stdout_closed = True

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        if not (self.killed or self.stdout_closed):
            raise AssertionError(
                "wait() called on a still-alive/still-piped process — this is exactly "
                "the pipe-deadlock regression (review N1): kill/close must happen BEFORE wait()"
            )
        if timeout is None:
            raise AssertionError(
                "wait() called with no timeout — an unbounded wait can hang the runner forever"
            )
        return self.returncode if self.returncode is not None else 0


@LOCK_REGRESSION_TIMEOUT
def test_run_claude_finally_reaps_safely_on_genuine_mid_loop_exception(
    cloud_runner, monkeypatch, tmp_path,
):
    """review N1 (and the rewrite of the old, misnamed I3 test): the raise must
    happen while claude is STILL RUNNING and stdout is STILL UNDRAINED — more
    than TRANSCRIPT batch-worth of events queued so `flush()` fires INSIDE the
    loop (not just in the `finally`), and the underlying iterator still has
    unconsumed lines when it raises, proving the loop genuinely exited early.
    """
    # 11 tool_use events: the 10th triggers the in-loop `flush()` (batch >= 10),
    # which is where `emit` raises — with a further (unconsumed) line still
    # queued behind it in the fake's iterator.
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "sess-crash"}) + "\n"]
    for i in range(11):
        lines.append(json.dumps({
            "type": "assistant", "session_id": "sess-crash",
            "message": {"content": [{"type": "tool_use", "name": f"Bash{i}"}]},
        }) + "\n")
    fake_proc = _HangingFakeProc(lines)
    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: fake_proc)

    transcript_calls = []

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            transcript_calls.append(body)
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)

    emitted = []

    def crashing_emit(batch):
        emitted.append(batch)
        raise RuntimeError("socket dropped")

    ok, text, cli_session_id = cloud_runner.run_claude(
        "hi", "turn-crash", crashing_emit, cwd=tmp_path,
    )

    # The crash genuinely happened mid-loop: at least one line was still
    # unconsumed in the fake's stdout iterator when `emit` raised — proving
    # early exit, not a full drain (the defect the old, misnamed test missed).
    remaining = list(fake_proc._iter)
    assert len(remaining) > 0, "loop must exit before exhausting stdout to be a genuine mid-loop case"
    assert len(emitted) == 1  # flush() ran exactly once, from inside the loop
    # The fix's contract, proven via _HangingFakeProc's own assertions: kill()
    # or stdout.close() happened, and wait() was given a bounded timeout —
    # otherwise the fake itself would have raised inside run_claude and this
    # call would have propagated an AssertionError instead of returning.
    assert fake_proc.killed or fake_proc.stdout_closed
    assert ok is False
    assert "socket dropped" in text
    assert cli_session_id == "sess-crash"  # captured before the crash, not lost
    # Whatever WAS buffered before the crash made it to the transcript, in
    # order, as an exact PREFIX of the full stream (the trailing lines never
    # read off stdout are correctly absent — this loop crashed, it didn't
    # invent content it never saw).
    shipped = [line for call in transcript_calls for line in call["lines"]]
    all_stripped = [line.strip() for line in lines]
    assert shipped, "the finally's flush_transcript() must have shipped something"
    assert shipped == all_stripped[: len(shipped)]
    assert len(shipped) < len(all_stripped), "some lines must remain unshipped — a genuine early exit"


# ── genuine concurrency tests (review: "two real concurrency tests over ─────
# thirteen structural ones") ─────────────────────────────────────────────────

@LOCK_REGRESSION_TIMEOUT
def test_run_claude_concurrent_flushes_never_reorder_duplicate_or_drop(cloud_runner, monkeypatch, tmp_path):
    """Drives the READ LOOP's own byte-triggered flush and the PERIODIC
    background thread's flush against each other for real: a tiny
    TRANSCRIPT_FLUSH_SECONDS makes the periodic thread fire almost
    continuously while a fast-yielding fake stdout keeps crossing the byte
    threshold too, so both producers of flush_transcript() genuinely race for
    the same buffer across ~40 lines. Asserts the property the locking is
    FOR: every line reaches the server in original order, exactly once, and
    batch_ids are unique (no accidental re-post) with the seq component
    strictly increasing across the whole run — i.e. no reorder, no
    duplication, no loss, even though two threads are contending for the
    buffer and the post-serialization lock throughout."""
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_SECONDS", 0.01)
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_BYTES", 200)  # small: forces many flushes

    n = 40
    lines = [
        json.dumps({
            "type": "assistant", "session_id": "sess-race",
            "message": {"content": [{"type": "text", "text": f"line-{i:03d}-" + ("x" * 30)}]},
        }) + "\n"
        for i in range(n)
    ]
    lines.append(json.dumps({"type": "result", "session_id": "sess-race", "result": "done", "is_error": False}) + "\n")

    class _RealisticSlowStdout:
        """A real generator (unlike a bare list) so the periodic thread gets
        genuine wall-clock windows to race against the read loop — a tiny
        sleep per line lets the 0.01s periodic timer fire multiple times
        while lines are still arriving, without making the test slow."""
        def __init__(self, src):
            self._src = src

        def __iter__(self):
            for line in self._src:
                time.sleep(0.002)
                yield line

    class _FakeProcRealistic:
        def __init__(self, src):
            self.stdout = _RealisticSlowStdout(src)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: _FakeProcRealistic(lines))

    transcript_calls = []
    calls_lock = threading.Lock()

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            # Jitter (review N6): without this, the fake server returns so
            # fast that the window between seq-allocation and the call being
            # RECORDED is nanoseconds — a real reorder essentially never
            # materializes even with transcript_post_lock deleted entirely
            # (verified: that mutant passed this test 5/5 before this jitter
            # was added). Delaying every other (odd-seq) call gives a missing
            # lock a real chance to let a later, even-seq call's POST land
            # and get recorded FIRST — turning "no lock" into an actually
            # observable, not just theoretical, failure.
            seq = int(body["batch_id"].split(":")[2])
            if seq % 2:
                time.sleep(0.02)
            with calls_lock:
                transcript_calls.append(body)
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)

    ok, _text, _sid = cloud_runner.run_claude(
        "hi", "turn-race", lambda batch: None, cwd=tmp_path,
    )
    assert ok is True

    # No loss, no reorder, no duplication: the concatenation of every batch's
    # lines, IN THE ORDER THE FAKE SERVER RECEIVED THEM, must equal the
    # original stream exactly.
    shipped = [line for call in transcript_calls for line in call["lines"]]
    assert shipped == [line.strip() for line in lines]

    # batch_ids: all unique (no duplicate posts), all sharing one attempt id,
    # and seq strictly increasing in RECEIPT order (proves posts landed in
    # swap order despite two threads producing them).
    batch_ids = [call["batch_id"] for call in transcript_calls]
    assert len(batch_ids) == len(set(batch_ids)), "a batch_id repeated — duplicate/racy post"
    parsed = [bid.split(":") for bid in batch_ids]
    assert len({p[1] for p in parsed}) == 1, "attempt id must be stable across the whole run"
    seqs = [int(p[2]) for p in parsed]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1)), "seq must be gap-free and in receipt order"
    assert len(transcript_calls) > 1, "expected genuine contention to produce more than one flush"


@LOCK_REGRESSION_TIMEOUT
def test_run_claude_append_not_blocked_by_a_slow_transcript_post(cloud_runner, monkeypatch, tmp_path):
    """review N2 (hardened per round-2 N7): before the fix, every per-line
    append held `transcript_lock` for the ENTIRE duration of a flush's
    network POST (including retries). This drives a slow POST specifically
    from the PERIODIC thread while the read loop keeps consuming fresh lines
    on the main thread.

    Asserts STRUCTURALLY rather than on total elapsed wall-clock time (a
    round-2 finding: a 0.43-0.44s vs. 0.5s bound is only ~13% headroom, thin
    enough to flake on a loaded/shared CI runner): records the timestamp each
    line is CONSUMED off stdout, and requires the LAST one to land before the
    slow POST returns. If per-line appends were blocked behind the in-flight
    POST, no further line could be consumed until it finished, so the last
    line's timestamp would fall AFTER the POST's return — this cannot flake
    on system load the way a total-duration bound can, because it compares
    two events on the SAME timeline rather than an absolute duration against
    a fixed constant.
    """
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_SECONDS", 0.02)
    monkeypatch.setattr(cloud_runner, "TRANSCRIPT_FLUSH_BYTES", 10**9)  # never byte-trigger; isolate the timer

    SLOW_POST_SECONDS = 0.4
    LINE_DELAY_SECONDS = 0.01
    N_LINES = 20  # ~0.2s of line-feeding if unblocked; << SLOW_POST_SECONDS

    lines = [
        json.dumps({
            "type": "assistant", "session_id": "sess-slow-post",
            "message": {"content": [{"type": "text", "text": f"l{i}"}]},
        }) + "\n"
        for i in range(N_LINES)
    ]
    lines.append(json.dumps(
        {"type": "result", "session_id": "sess-slow-post", "result": "done", "is_error": False}
    ) + "\n")

    line_consumed_at: list[float] = []

    class _SlowLineStdout:
        def __iter__(self):
            for line in lines:
                time.sleep(LINE_DELAY_SECONDS)
                line_consumed_at.append(time.monotonic())
                yield line

    class _FakeProcSlowLines:
        def __init__(self):
            self.stdout = _SlowLineStdout()
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(cloud_runner.subprocess, "Popen", lambda *a, **k: _FakeProcSlowLines())

    first_post_started = threading.Event()
    post_finished_at: list[float] = []
    call_count = [0]
    calls_lock = threading.Lock()

    def fake_api(method, path, body=None):
        if path.endswith("/transcript"):
            with calls_lock:
                call_count[0] += 1
                is_first = call_count[0] == 1
            if is_first:
                first_post_started.set()
                time.sleep(SLOW_POST_SECONDS)  # simulate a slow/struggling canopy-web
                post_finished_at.append(time.monotonic())
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cloud_runner, "_api", fake_api)

    ok, _text, _sid = cloud_runner.run_claude(
        "hi", "turn-slow-post", lambda batch: None, cwd=tmp_path,
    )

    assert ok is True
    assert first_post_started.is_set(), "the periodic flush never fired a POST to slow down"
    assert post_finished_at, "the slow POST never returned"
    assert line_consumed_at, "no lines were consumed"
    assert line_consumed_at[-1] < post_finished_at[0], (
        "the LAST line was only consumed AFTER the slow POST returned — "
        "the read loop looks blocked behind it (review N2)"
    )
