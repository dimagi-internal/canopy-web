"""A cloud AGENT turn gets a canopy Session, recorded the moment it starts.

Before this, only CHAT turns on the cloud runner recorded a session: an agent
turn (a schedule, a dispatch, an email-less api turn) ran with no session, so
the live stream tail never saw it, the Turns page could not link it to a chat,
its finish carried no session key, and the agent's close-out report could not
find it — every cloud close-out became a second, report-only turn row.

The laptop runner already does this (execute.py records `<agent>:<turn id>`); the
cloud runner now does the same, under either executor, and records it as soon as
the CLI session id is known so the session streams WHILE the turn runs.
"""
from __future__ import annotations

import threading

import pytest

TID = "609d0a02-b763-4d91-aaaf-7b8bf9fdcffd"


def _agent_turn(**kw):
    d = {"id": TID, "agent_slug": "echo", "prompt": "/echo:turn",
         "origin_ref": {"slot": "2026-10-01T17:00:00+00:00", "schedule_name": "Daily turn"}}
    d.update(kw)
    return d


# ── which thread a turn records under ────────────────────────────────────────

def test_an_agent_turn_records_under_agent_and_turn_id(cloud_runner):
    assert cloud_runner._agent_thread_key(_agent_turn()) == f"echo:{TID}"


def test_a_named_thread_still_gets_one_session_per_cloud_agent_turn(cloud_runner):
    """A cloud agent turn never resumes, so sharing a session across a named
    thread would only re-point one turn's binding at another's transcript."""
    turn = _agent_turn(origin_ref={"thread_key": "dispatch-ace-0fccbf2661bb"})
    assert cloud_runner._agent_thread_key(turn) == f"echo:{TID}"


def test_a_chat_turn_keeps_its_own_thread(cloud_runner):
    chat = _agent_turn(origin_ref={"chat_session_id": "abc", "thread_key": "abc"})
    assert cloud_runner._agent_thread_key(chat) == ""
    assert cloud_runner._session_thread_key(chat) == "abc"


def test_recording_an_agent_turn_creates_its_session_server_side(cloud_runner, monkeypatch):
    posts = []
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, path, body=None, **k: posts.append((path, body)) or (200, {}))
    cloud_runner._record_session_resume("r-1", _agent_turn(), "cli-1")
    assert posts == [("/runners/r-1/record-session", {
        "thread_key": f"echo:{TID}", "session_key": "cli-1", "session_id": "cli-1",
        "title": "Daily turn", "turn_id": TID, "agent_slug": "echo",
    })]


def test_an_agent_session_is_named_for_a_person_not_by_its_uuid(cloud_runner):
    assert cloud_runner._agent_session_title(_agent_turn()) == "Daily turn"
    assert cloud_runner._agent_session_title(
        _agent_turn(origin_ref={}, prompt="\nFix brief from Ada\nmore")) == "Fix brief from Ada"


def test_a_command_prompt_is_not_a_session_name(cloud_runner):
    """2026-10-03: ACE's email session was titled "/ace:turn --thread 1a0f…"."""
    email = _agent_turn(origin_ref={"subject": "Latest on workflows", "thread_id": "1a0f"},
                        prompt="/ace:turn --thread 1a0f24bf9b830273")
    assert cloud_runner._agent_session_title(email) == "Latest on workflows"
    bare = _agent_turn(origin_ref={}, prompt="/ace:turn --thread 1a0f24bf9b830273")
    assert cloud_runner._agent_session_title(bare) == ""
    brief = _agent_turn(origin_ref={}, prompt="/eva:turn — catch-up brief from Jonathan")
    assert cloud_runner._agent_session_title(brief) == "catch-up brief from Jonathan"


def test_a_chat_turn_sends_no_title(cloud_runner, monkeypatch):
    posts = []
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, path, body=None, **k: posts.append(body) or (200, {}))
    chat = _agent_turn(origin_ref={"chat_session_id": "abc", "thread_key": "abc"})
    cloud_runner._record_session_resume("r-1", chat, "cli-1")
    assert "title" not in posts[0]


# ── the stream tail finds an agent turn's transcript ─────────────────────────

def _transcript(tmp_path, cloud_runner, cwd, session_key):
    from canopy_transcript import paths  # the same core the runner resolves with
    f = tmp_path / "projects" / paths.encode_project_dir(cwd) / f"{session_key}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"type": "user", "message": {"content": "hi"}}\n')
    return f


@pytest.fixture()
def roots(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path / "agents"))
    if cloud_runner._transcript_core() is None:
        pytest.skip("canopy_transcript not importable here")
    return tmp_path


def test_an_agent_sessions_transcript_is_found_in_the_agent_clone(cloud_runner, roots):
    """An agent turn runs in AGENT_ROOT/<agent>, not WORK_DIR/sessions/<id>; the
    descriptor's project (the session's emdash_project) names the agent."""
    f = _transcript(roots, cloud_runner, roots / "agents" / "echo", "cli-1")
    assert cloud_runner._session_transcript_path("sess-1", "cli-1", "echo") == f
    assert cloud_runner._session_transcript_path("sess-1", "cli-1") is None


def test_a_chat_sessions_transcript_is_still_found_in_its_own_dir(cloud_runner, roots):
    f = _transcript(roots, cloud_runner, roots / "work" / "sessions" / "sess-1", "cli-2")
    assert cloud_runner._session_transcript_path("sess-1", "cli-2", "echo") == f


def test_a_hostile_project_never_becomes_a_path(cloud_runner, roots):
    _transcript(roots, cloud_runner, roots / "agents" / ".." / "x", "cli-3")
    assert cloud_runner._session_transcript_path("sess-1", "cli-3", "../x") is None


# ── recorded when the session starts, keyed on finish ────────────────────────

@pytest.fixture()
def run(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(cloud_runner, "_start_lease_renewal", lambda rid, tid: threading.Event())
    monkeypatch.setattr(cloud_runner, "_child_safe_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(cloud_runner, "_ship_transcript_rows", lambda *a, **k: None)
    timeline = []
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, path, body=None, **k: timeline.append((path, body)) or (200, {}))
    return cloud_runner, timeline


def _execute_that_starts_a_session(cloud_runner, timeline, sid="cli-1", ok=True):
    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        cloud_runner._announce_session(turn_id, sid)
        timeline.append(("<agent working>", None))
        return ok, "Nothing to do.", sid
    return fake_execute


def test_the_session_is_recorded_while_the_turn_runs(run, monkeypatch):
    mod, timeline = run
    monkeypatch.setattr(mod, "execute_prompt", _execute_that_starts_a_session(mod, timeline))
    mod._run_turn("r-1", _agent_turn())
    paths = [p for p, _ in timeline]
    assert paths.index("/runners/r-1/record-session") < paths.index("<agent working>"), (
        "recorded only after the turn — the live tail could not follow it")
    assert paths.count("/runners/r-1/record-session") == 1, "recorded again after the turn"


def test_finish_carries_the_session_key(run, monkeypatch):
    mod, timeline = run
    monkeypatch.setattr(mod, "execute_prompt", _execute_that_starts_a_session(mod, timeline))
    mod._run_turn("r-1", _agent_turn())
    finish = [b for p, b in timeline if p == f"/turns/{TID}/finish"]
    assert finish == [{"status": "done", "result_note": "Nothing to do.", "session_key": "cli-1"}]


def test_a_failed_turn_that_reached_an_agent_says_so(run, monkeypatch):
    """An empty key on a FAILED turn makes the server re-run it as sessionless.
    A session existed, so that retry would repeat work blind."""
    mod, timeline = run
    monkeypatch.setattr(mod, "execute_prompt",
                        _execute_that_starts_a_session(mod, timeline, ok=False))
    mod._run_turn("r-1", _agent_turn())
    finish = [b for p, b in timeline if p == f"/turns/{TID}/finish"][0]
    assert finish["status"] == "failed" and finish["session_key"] == "cli-1"


def test_a_turn_that_never_started_a_session_finishes_without_a_key(run, monkeypatch):
    mod, timeline = run
    monkeypatch.setattr(mod, "execute_prompt",
                        lambda *a, **k: (False, "emdash create failed", ""))
    mod._run_turn("r-1", _agent_turn())
    finish = [b for p, b in timeline if p == f"/turns/{TID}/finish"][0]
    assert "session_key" not in finish
    assert not any(p.endswith("/record-session") for p, _ in timeline)


def test_the_hook_is_gone_after_the_turn(run, monkeypatch):
    mod, timeline = run
    monkeypatch.setattr(mod, "execute_prompt", _execute_that_starts_a_session(mod, timeline))
    mod._run_turn("r-1", _agent_turn())
    assert TID not in mod._SESSION_HOOKS


# ── both executors announce the session as soon as they know it ──────────────

class _Reducer:
    assistant_text = "done"
    rate_limit = None

    def apply(self, update):
        return None

    def reset_stream_state(self):
        pass


class _Done:
    def result(self, timeout=None):
        return {"stopReason": "end_turn"}


def test_acp_announces_the_session_before_prompting(cloud_runner, monkeypatch, tmp_path):
    order = []

    class Agent:
        session_id = "acp-1"

        def __init__(self, cwd, env, on_update):
            pass

        def start(self):
            pass

        def new_session(self, timeout=None):
            order.append(f"timeout:{timeout}")
            return self.session_id

        def prompt(self, _p):
            order.append("prompt")
            return _Done()

        def cancel(self):
            pass

        def close(self):
            pass

    class Core:
        AcpAgent = Agent
        UpdateReducer = _Reducer

    monkeypatch.setattr(cloud_runner, "_acp_core", lambda: Core)
    monkeypatch.setattr(cloud_runner, "_agent_env", lambda slug: {})
    cloud_runner._SESSION_HOOKS["t-acp"] = lambda sid: order.append(f"session:{sid}")
    try:
        ok, _, sid = cloud_runner.run_acp("/echo:turn", "t-acp", lambda e: None, cwd=tmp_path)
    finally:
        cloud_runner._SESSION_HOOKS.pop("t-acp", None)
    assert (ok, sid) == (True, "acp-1")
    # Session start gets its own, generous timeout — not canopy_acp's 120s default
    # (eva's environment sometimes needs ~125s to boot; 2026-10-04).
    assert order == [f"timeout:{cloud_runner.ACP_SESSION_START_TIMEOUT_SECONDS}",
                     "session:acp-1", "prompt"]
    assert cloud_runner.ACP_SESSION_START_TIMEOUT_SECONDS >= 300


def test_announcing_with_no_hook_or_a_failing_hook_is_harmless(cloud_runner):
    cloud_runner._announce_session("nobody", "x")  # no hook registered
    cloud_runner._SESSION_HOOKS["t-bad"] = lambda sid: 1 / 0
    try:
        cloud_runner._announce_session("t-bad", "x")  # must not raise
    finally:
        cloud_runner._SESSION_HOOKS.pop("t-bad", None)


# ── a reply in an agent turn's chat continues it ─────────────────────────────
# The agent turn ran (and wrote its session) in AGENT_ROOT/<agent>; a reply is a
# session turn in WORK_DIR/sessions/<id>. Claude finds a resume target by cwd, so
# without adoption the reply started fresh with none of the turn's context.

def test_a_reply_adopts_the_agent_turns_session_so_it_can_resume(cloud_runner, roots):
    agent_dir = roots / "agents" / "echo"
    chat_dir = roots / "work" / "sessions" / "sess-1"
    src = _transcript(roots, cloud_runner, agent_dir, "cli-1")
    assert cloud_runner._resume_target_exists(chat_dir, "cli-1") is False

    assert cloud_runner._adopt_resume_transcript(chat_dir, "cli-1", "echo") is True
    assert cloud_runner._resume_target_exists(chat_dir, "cli-1") is True
    assert src.is_file(), "the agent clone's copy is a record — copy, never move"
    # The stream tail now reads the chat dir's copy, which the resume extends.
    assert cloud_runner._session_transcript_path("sess-1", "cli-1", "echo").parent.name \
        != src.parent.name


def test_adoption_leaves_an_existing_transcript_alone(cloud_runner, roots):
    chat_dir = roots / "work" / "sessions" / "sess-2"
    _transcript(roots, cloud_runner, roots / "agents" / "echo", "cli-2")
    here = _transcript(roots, cloud_runner, chat_dir, "cli-2")
    here.write_text("already here\n")
    assert cloud_runner._adopt_resume_transcript(chat_dir, "cli-2", "echo") is False
    assert here.read_text() == "already here\n"


def test_adoption_needs_a_source_and_a_safe_agent(cloud_runner, roots):
    chat_dir = roots / "work" / "sessions" / "sess-3"
    assert cloud_runner._adopt_resume_transcript(chat_dir, "missing", "echo") is False
    assert cloud_runner._adopt_resume_transcript(chat_dir, "cli-3", "../echo") is False
    assert cloud_runner._adopt_resume_transcript(chat_dir, "", "echo") is False


def test_a_resuming_turn_adopts_before_it_runs(run, monkeypatch, tmp_path):
    mod, timeline = run
    adopted = []
    monkeypatch.setattr(mod, "_adopt_resume_transcript",
                        lambda cwd, sid, slug: adopted.append((sid, slug)) or True)
    monkeypatch.setattr(mod, "execute_prompt",
                        lambda prompt, tid, emit, cwd=None, agent_slug=None, resume_session_id=None:
                        (timeline.append(("run", resume_session_id)) or True, "ok", "cli-1"))
    chat = _agent_turn(origin_ref={"chat_session_id": "sess-1", "thread_key": f"echo:{TID}"},
                       _resume_id="cli-1")
    mod._run_turn("r-1", chat)
    assert adopted == [("cli-1", "echo")]
    assert ("run", "cli-1") in timeline


def test_a_dropped_acp_connection_carries_the_adapters_exit(cloud_runner, monkeypatch, tmp_path):
    """The failure note names how the adapter died, so a dropped connection is
    diagnosable from the Turns page instead of needing the box's journal."""

    class _Dropped:
        def result(self, timeout=None):
            raise RuntimeError("ACP connection closed before a reply arrived")

    class Agent:
        session_id = "acp-2"

        def __init__(self, cwd, env, on_update):
            pass

        def start(self):
            pass

        def new_session(self, timeout=None):
            return self.session_id

        def prompt(self, _p):
            return _Dropped()

        def exit_report(self):
            return "adapter killed by signal 9; no stderr"

        def cancel(self):
            pass

        def close(self):
            pass

    class Core:
        AcpAgent = Agent
        UpdateReducer = _Reducer

    monkeypatch.setattr(cloud_runner, "_acp_core", lambda: Core)
    monkeypatch.setattr(cloud_runner, "_agent_env", lambda slug: {})
    ok, note, sid = cloud_runner.run_acp("/hal:turn", "t-drop", lambda e: None, cwd=tmp_path)
    assert (ok, sid) == (False, "acp-2")
    assert note == ("runner error (acp): ACP connection closed before a reply arrived "
                    "(adapter killed by signal 9; no stderr)")
