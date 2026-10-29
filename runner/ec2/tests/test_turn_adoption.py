"""Turns outlive the daemon: a restarted runner adopts the turns it left running.

The pieces, each driven for real where it can be: the RELAY (`--turn-host`, a real
process that runs a fake CLI and records its stream and exit), the ADOPTION a new
daemon does at start (`adopt_turns`), and the SUPERVISOR that swaps the daemon on an
update without stopping the unit (`supervise_main`, with fake children).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

import pytest

TURN = "0f3c2a1e-1111-4222-8333-444455556666"


def _fake_cli(tmp_path: pathlib.Path, *, gate: pathlib.Path | None = None, code: int = 0) -> list[str]:
    """A `claude -p` stand-in: two assistant lines, then (once `gate` exists) its result."""
    script = tmp_path / "fake_claude.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        def say(o):
            print(json.dumps(o), flush=True)
        say({{"type": "system", "subtype": "init", "session_id": "sess-1"}})
        say({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "first"}}]}}}})
        say({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "second"}}]}}}})
        gate = {str(gate) if gate else ''!r}
        while gate and not os.path.exists(gate):
            time.sleep(0.05)
        say({{"type": "result", "result": "all done", "is_error": False}})
        sys.exit({code})
    """))
    return [sys.executable, str(script)]


@pytest.fixture
def cr(cloud_runner, tmp_path, monkeypatch, request):
    # Short, so the relay's unix socket fits its ~104-byte path cap on a laptop too.
    short = pathlib.Path(tempfile.mkdtemp(prefix="cr-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(short, ignore_errors=True))
    monkeypatch.setattr(cloud_runner, "TURN_STATE_ROOT", short / "turns")
    monkeypatch.setattr(cloud_runner, "RELAY_START_TIMEOUT", 15.0)
    calls: list[tuple] = []

    def fake_api(method, path, body=None, **_kw):
        calls.append((method, path, body))
        return 200, {}
    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    monkeypatch.setattr(cloud_runner, "_post_transcript_batch", lambda *a, **k: True)
    cloud_runner._calls = calls
    return cloud_runner


def _wait(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _lifecycle_events(cr) -> list[str]:
    return [e["payload"]["event"] for (_m, p, b) in cr._calls if p.endswith("/events") and b
            for e in b["events"] if e["kind"] == "lifecycle"]


def test_the_relay_records_the_stream_and_the_exit_without_the_daemon(cr, tmp_path):
    proc = cr.RelayProc.spawn(TURN, _fake_cli(tmp_path), cwd=tmp_path, env=dict(os.environ),
                              stdin=False, executor="cli")
    assert proc.wait(timeout=15) == 0
    wire = (proc.dir / "wire.log").read_text().splitlines()
    assert [w.split("\t", 1)[0] for w in wire] == ["o"] * 4
    assert json.loads((proc.dir / "exit.json").read_text())["code"] == 0
    # The CLI was the RELAY's child, in a session of its own: not this process's.
    info = json.loads((proc.dir / "host.json").read_text())
    assert info["child_pid"] != os.getpid() and info["host_pid"] != os.getpid()
    lines = list(cr.RelayProc.attach(proc.dir).stdout)
    assert json.loads(lines[-1])["result"] == "all done"


def test_a_restarted_daemon_reads_on_from_where_the_old_one_was(cr, tmp_path):
    """Adopted mid-stream: the whole stream is re-read for its state (session, result),
    but what the old daemon already emitted is not emitted again."""
    gate = tmp_path / "gate"
    proc = cr.RelayProc.spawn(TURN, _fake_cli(tmp_path, gate=gate), cwd=tmp_path,
                              env=dict(os.environ), stdin=False, executor="cli")
    wire = proc.dir / "wire.log"
    assert _wait(lambda: len(wire.read_text().splitlines()) >= 3)
    # The old daemon emitted the first two lines' events, then went away.
    proc.record_progress({"attempt_id": "a1", "seq": 1, "posted": 2, "emitted": 2})
    proc = None

    adopted = cr.RelayProc.attach(cr.TURN_STATE_ROOT / TURN / "attempt-1")
    emitted: list = []
    gate.write_text("go")
    ok, text, sid, lines, err = cr._stream_claude(
        adopted, TURN, emitted.extend, adopted.stderr_path,
        progress=json.loads((adopted.dir / "progress.json").read_text()))
    assert (ok, text, sid, err) == (True, "all done", "sess-1", None)
    assert lines == 4
    # "first" was line 2: emitted before the restart, not again.
    assert [e["payload"]["text"] for e in emitted if e["kind"] == "assistant"] == ["second"]


def _state(cr, tmp_path, *, phase="running", attempt="attempt-1", **extra):
    d = cr.TURN_STATE_ROOT / TURN
    d.mkdir(parents=True, exist_ok=True)
    state = {"turn": {"id": TURN, "target": "agent:echo"}, "runner_id": "rid", "phase": phase,
             "adoptable": True, "attempt": attempt, "executor": "cli", "slug": "echo",
             "cwd": str(tmp_path), "started_at": time.time(), **extra}
    (d / "turn.json").write_text(json.dumps(state))
    return d


def _quiet_conclusion(cr, monkeypatch):
    import threading as _t
    for name in ("_record_session_cwd", "_record_session_resume", "_ship_transcript_rows",
                 "_release_agent_worktree", "remove_turn_env", "_remove_agent_token",
                 "_remove_scoped_token"):
        monkeypatch.setattr(cr, name, lambda *a, **k: None)
    monkeypatch.setattr(cr, "_start_lease_renewal", lambda rid, tid: _t.Event())


def test_a_live_turn_is_adopted_and_reported_as_the_old_daemon_would(cr, tmp_path, monkeypatch):
    _quiet_conclusion(cr, monkeypatch)
    gate = tmp_path / "gate"
    # Started by a daemon that is gone: nothing here holds a handle on it.
    cr.RelayProc.spawn(TURN, _fake_cli(tmp_path, gate=gate), cwd=tmp_path,
                       env=dict(os.environ), stdin=False, executor="cli")
    _state(cr, tmp_path, cli_session_id="sess-1")

    summary = cr.adopt_turns("rid")
    assert [a.split()[0] for a in summary["adopted"]] == [TURN[:8]]
    # In flight here at once: its lease rides this daemon's heartbeat.
    assert TURN in cr._in_flight_ids()
    gate.write_text("go")
    assert _wait(lambda: TURN not in cr._in_flight_ids())
    finishes = [b for (_m, p, b) in cr._calls if p == f"/turns/{TURN}/finish"]
    assert len(finishes) == 1
    assert finishes[0]["status"] == "done"
    assert finishes[0]["session_key"] == "sess-1"
    assert "all done" in finishes[0]["result_note"]
    assert {"adopted", "exited", "reported"} <= set(_lifecycle_events(cr))
    # Its state goes once it is reported; nothing of it is left to adopt twice.
    assert not (cr.TURN_STATE_ROOT / TURN).exists()


def test_a_turn_whose_process_is_gone_fails_with_the_reason(cr, tmp_path, monkeypatch):
    _quiet_conclusion(cr, monkeypatch)
    d = _state(cr, tmp_path, cli_session_id="sess-9")
    attempt = d / "attempt-1"
    attempt.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (attempt / "spawn.json").write_text(json.dumps({"argv": ["claude"], "stdin": False}))
    (attempt / "host.json").write_text(json.dumps({"host_pid": dead.pid, "child_pid": dead.pid}))

    summary = cr.adopt_turns("rid")
    assert summary["adopted"] == [] and len(summary["reported"]) == 1
    finish = next(b for (_m, p, b) in cr._calls if p == f"/turns/{TURN}/finish")
    assert finish["status"] == "failed"
    assert "left no exit status" in finish["result_note"]
    assert finish["session_key"] == "sess-9"  # it reached the agent: never re-run blind
    assert "lost" in _lifecycle_events(cr)
    assert not d.exists()


def test_a_turn_cut_off_during_its_setup_fails_without_a_session(cr, tmp_path, monkeypatch):
    _quiet_conclusion(cr, monkeypatch)
    _state(cr, tmp_path, phase="setup", attempt=None)
    cr.adopt_turns("rid")
    finish = next(b for (_m, p, b) in cr._calls if p == f"/turns/{TURN}/finish")
    assert finish["status"] == "failed" and "being set up" in finish["result_note"]
    assert "session_key" not in finish  # sessionless: canopy-web may run it again


def test_the_start_sweep_leaves_an_adopted_turns_env_alone(cr, tmp_path, monkeypatch):
    root = tmp_path / "envs"
    (root / TURN).mkdir(parents=True)
    (root / "someone-elses-leftover").mkdir()
    monkeypatch.setattr(cr, "_turn_env_root", lambda create=True: root)
    cr._ADOPTED.add(TURN)
    cr.sweep_turn_envs()
    assert (root / TURN).exists() and not (root / "someone-elses-leftover").exists()


def test_a_turns_state_file_holds_no_credential(cr):
    kept = cr._without_secrets({"id": TURN, "mcp_token": "x", "chat_key": "k",
                                "origin_ref": {"api_token": "y", "from": "a@b"}})
    assert kept == {"id": TURN, "origin_ref": {"from": "a@b"}}


def test_a_handoff_leaves_without_waiting_on_its_turns(cr, monkeypatch):
    blocker = threading.Event()
    t = threading.Thread(target=blocker.wait, daemon=True)
    t.start()
    cr._IN_FLIGHT[TURN] = t
    monkeypatch.setattr(cr, "_HANDOFF", True)
    started = time.monotonic()
    cr._finish_stop(drain_seconds=30)
    assert time.monotonic() - started < 5
    assert t.is_alive()
    blocker.set()


def test_only_an_unconfined_turn_on_a_supervised_box_is_adoptable(cr, monkeypatch):
    monkeypatch.setattr(cr, "_capability", lambda turn: None)
    assert not cr.adoptable_turn({"id": TURN})  # no supervisor: the unit's restart kills it
    monkeypatch.setenv("CANOPY_RUNNER_SUPERVISED", "1")
    assert cr.adoptable_turn({"id": TURN})
    monkeypatch.setattr(cr, "_capability", lambda turn: "ask")
    assert not cr.adoptable_turn({"id": TURN})


def test_the_in_flight_marker_counts_what_a_handoff_cannot_carry(cr, tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_RUNNER_SUPERVISED", "1")
    monkeypatch.setattr(cr, "IN_FLIGHT_FILE", tmp_path / "in-flight")
    other = "0f3c2a1e-9999-4222-8333-444455556666"
    cr._update_turn_state(TURN, adoptable=True)
    cr._IN_FLIGHT.update({TURN: None, other: None})
    cr._mark_in_flight(2)
    marker = json.loads((tmp_path / "in-flight").read_text())
    assert marker["count"] == 2 and marker["pinned"] == 1 and marker["handoff"] == 1


# -- the supervisor ----------------------------------------------------------------

class _Child:
    def __init__(self, n):
        self.pid = 1000 + n
        self.signals: list[int] = []

    def poll(self):
        return 0 if self.signals else None

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append(signal.SIGKILL)


def test_the_supervisor_swaps_the_daemon_on_sighup_and_stops_on_sigterm(cr, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SUPERVISOR_FILE", tmp_path / "supervisor.json")
    children: list[_Child] = []
    spawned_with: list = []

    def popen(argv, env):
        spawned_with.append((argv, env.get("CANOPY_RUNNER_SUPERVISED")))
        children.append(_Child(len(children)))
        return children[-1]

    ticks = {"n": 0}

    def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] == 2:
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)    # an update
        if ticks["n"] == 6:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # the unit stops

    saved = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    try:
        assert cr.supervise_main(popen=popen, sleep=sleep) == 0
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert len(children) == 2
    assert all(argv[-1] == "--daemon" and flag == "1" for argv, flag in spawned_with)
    # The old daemon was asked to HAND OFF (leave its turns), the new one to STOP.
    assert children[0].signals == [signal.SIGUSR1]
    assert children[1].signals == [signal.SIGTERM]


def _fake_adapter(tmp_path: pathlib.Path, gate: pathlib.Path) -> list[str]:
    """A claude-agent-acp stand-in speaking JSON-RPC on its stdio."""
    script = tmp_path / "fake_acp.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def say(text):
            send({{"jsonrpc": "2.0", "method": "session/update", "params": {{"sessionId": "s1",
                  "update": {{"sessionUpdate": "agent_message_chunk",
                             "content": {{"type": "text", "text": text}}}}}}}})
        for line in sys.stdin:
            msg = json.loads(line)
            m, i = msg.get("method"), msg.get("id")
            if m == "initialize":
                send({{"jsonrpc": "2.0", "id": i, "result": {{"protocolVersion": 1}}}})
            elif m == "session/new":
                send({{"jsonrpc": "2.0", "id": i, "result": {{"sessionId": "s1"}}}})
            elif m == "session/prompt":
                say("hello ")
                while not os.path.exists({str(gate)!r}):
                    time.sleep(0.05)
                say("world")
                send({{"jsonrpc": "2.0", "id": i, "result": {{"stopReason": "end_turn"}}}})
    """))
    return [sys.executable, str(script)]


def test_an_acp_turn_survives_its_daemon_and_is_finished_by_the_next(cr, tmp_path, monkeypatch):
    core = cr._acp_core()
    assert core is not None
    gate = tmp_path / "gate"
    spawn = lambda argv, cwd, env: cr.RelayProc.spawn(  # noqa: E731
        TURN, list(argv), cwd=cwd, env=dict(os.environ), stdin=True, executor="acp")
    agent = core.AcpAgent(cwd=tmp_path, argv=_fake_adapter(tmp_path, gate), spawn=spawn)
    agent.start()
    agent.new_session(timeout=10)
    agent.prompt("go")
    attempt = cr.TURN_STATE_ROOT / TURN / "attempt-1"
    assert _wait(lambda: "hello" in (attempt / "wire.log").read_text())
    # The old daemon is gone mid-turn (its connection is simply never used again).
    monkeypatch.setattr(cr, "_transcript_fault", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_ask_human_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(cr, "TRANSCRIPT_FLUSH_SECONDS", 0.1)
    emitted: list = []
    gate.write_text("go")
    ok, text, sid = cr._adopt_acp(TURN, cr.RelayProc.attach(attempt), emitted.extend, tmp_path,
                                  {"turn": {"id": TURN}, "started_at": time.time()})
    assert (ok, text, sid) == (True, "hello world", "s1")
    assert [e["payload"]["text"] for e in emitted if e["kind"] == "assistant"] == ["hello world"]


def test_each_transition_is_one_journal_line_and_one_turn_event(cr, capsys):
    cr._lifecycle(TURN, "adopted", pid=42, agent="ace", session="", executor="acp")
    line = next(l for l in capsys.readouterr().out.splitlines() if "turn-lifecycle" in l)
    record = json.loads(line.split("turn-lifecycle ", 1)[1])
    assert record == {"turn": TURN, "event": "adopted", "pid": 42, "agent": "ace", "executor": "acp"}
    (_m, path, body), = [c for c in cr._calls if c[1].endswith("/events")]
    assert path == f"/turns/{TURN}/events"
    assert body["events"][0] == {"kind": "lifecycle", "payload": {
        "event": "adopted", "pid": 42, "agent": "ace", "executor": "acp"}}


def test_a_failing_lease_renewal_is_said_once_per_streak(cr, monkeypatch):
    replies = iter([500, 0, 200])
    beats = threading.Event()

    def api(method, path, body=None, **_kw):
        cr._calls.append((method, path, body))
        if path.endswith("/heartbeat"):
            status = next(replies, 200)
            if status == 200:
                beats.set()
            return status, {}
        return 200, {}
    monkeypatch.setattr(cr, "_api", api)
    monkeypatch.setattr(cr, "LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(cr, "_heartbeat_body", lambda ids: {"active_turn_ids": ids})
    monkeypatch.setattr(cr, "_restage_delegated_google", lambda: None)
    stop = cr._start_lease_renewal("rid", TURN)
    assert beats.wait(5)
    stop.set()
    time.sleep(0.05)
    assert _lifecycle_events(cr) == ["lease_renew_failed", "lease_renewed"]


def test_the_updaters_decision_is_the_update_check(cr, tmp_path, monkeypatch):
    f = tmp_path / "update-last.json"
    monkeypatch.setattr(cr, "UPDATE_DECISION_FILE", f)
    f.write_text(json.dumps({"verdict": "installed", "detail": "aaa -> bbb: handed off",
                             "at": time.time()}))
    cr._update_check()
    check = cr._HEALTH["update"]
    assert check["status"] == "ok" and check["detail"].startswith("installed: aaa -> bbb: handed off")
    f.write_text(json.dumps({"verdict": "failed", "detail": "could not install", "at": time.time()}))
    cr._update_check()
    assert cr._HEALTH["update"]["status"] == "fail"


# ---- connections: an adopted turn keeps and refreshes its staged tokens ---------------

@pytest.fixture
def conns(cr, tmp_path, monkeypatch):
    """The adopting daemon's connection plumbing: canopy-web lists `salesforce`; the CLI
    writes the file. Everything else (the relay among it) runs for real."""
    home = tmp_path / "home"
    monkeypatch.setattr(cr.pathlib.Path, "home", lambda: home)
    monkeypatch.setattr(cr, "_bearer", lambda min_life=0: "runner-bearer")
    monkeypatch.setattr(cr, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(cr, "_CONN_HOLDS", {})
    monkeypatch.setattr(cr, "_ADOPTED", set())
    real_api, real_run = cr._api, cr.subprocess.run
    seen = {"listed": [], "cli": []}

    def api(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/connections"):
            seen["listed"].append(path)
            return 200, {"providers": ["salesforce"]}
        return real_api(method, path, body, **kw)

    def run(argv, **kw):
        if "connection-token" not in argv:
            return real_run(argv, **kw)
        seen["cli"].append(argv)
        f = home / ".canopy" / "connections" / argv[argv.index("--agent") + 1] / f"{argv[3]}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"access_token": "NEW", "expires_at": time.time() + 3600}))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(cr, "_api", api)
    monkeypatch.setattr(cr.subprocess, "run", run)

    def staged(slug="echo", provider="salesforce", token="OLD"):
        f = home / ".canopy" / "connections" / slug / f"{provider}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"access_token": token, "expires_at": time.time() + 3600}))
        return f
    return seen, staged


def test_an_adopted_agent_turn_keeps_restages_and_unstages_its_connections(cr, tmp_path, monkeypatch, conns):
    seen, staged = conns
    _quiet_conclusion(cr, monkeypatch)
    mine = staged()                                       # the previous daemon staged it
    other_provider = staged(provider="gmail")             # held by no adopted turn
    leftover = staged(slug="nobody")
    gate = tmp_path / "gate"
    cr.RelayProc.spawn(TURN, _fake_cli(tmp_path, gate=gate), cwd=tmp_path,
                       env=dict(os.environ), stdin=False, executor="cli")
    _state(cr, tmp_path, cli_session_id="sess-1", connections=["salesforce"], confined=False)

    cr.adopt_turns("rid")
    cr._clear_stale_connection_tokens()                   # main()'s order: adopt, then clear
    assert mine.exists()
    assert not other_provider.exists() and not leftover.exists()
    # Staged as a new turn's start: the same listing, the same CLI, for this turn.
    assert _wait(lambda: seen["cli"])
    assert seen["listed"] == [f"/runners/rid/turns/{TURN}/connections"]
    assert seen["cli"][0][-6:] == ["--runner-id", "rid", "--turn", TURN, "--min-life", "900"]
    assert json.loads(mine.read_text())["access_token"] == "NEW"
    assert cr._CONN_HOLDS[TURN][:1] == ("echo",) and cr._CONN_HOLDS[TURN][2] == {"salesforce"}
    # The heartbeat re-stages it near expiry, as any turn's.
    cr._restage_connections(TURN, now=time.time() + 3500)
    assert len(seen["cli"]) == 2 and "--turn" in seen["cli"][1]
    # Its end unstages like any turn's.
    gate.write_text("go")
    assert _wait(lambda: TURN not in cr._in_flight_ids())
    assert not mine.exists() and cr._CONN_HOLDS == {}


def test_an_adopted_confined_turn_stages_nothing(cr, tmp_path, monkeypatch, conns):
    seen, staged = conns
    _quiet_conclusion(cr, monkeypatch)
    f = staged()
    gate = tmp_path / "gate"
    cr.RelayProc.spawn(TURN, _fake_cli(tmp_path, gate=gate), cwd=tmp_path,
                       env=dict(os.environ), stdin=False, executor="cli")
    d = _state(cr, tmp_path, connections=["salesforce"])
    state = json.loads((d / "turn.json").read_text())
    state["turn"]["caller_context"] = {"profile": "confined"}
    (d / "turn.json").write_text(json.dumps(state))

    cr.adopt_turns("rid")
    cr._clear_stale_connection_tokens()
    assert not f.exists() and cr._CONN_HOLDS == {}
    gate.write_text("go")
    assert _wait(lambda: TURN not in cr._in_flight_ids())
    assert seen == {"listed": [], "cli": []} and cr._CONN_HOLDS == {}


def test_a_running_turn_records_its_providers_without_a_token(cr):
    assert cr._without_secrets({"connections": ["salesforce"], "confined": False}) == \
        {"connections": ["salesforce"], "confined": False}
