"""The host is what stands between an app and a person's conversation or a stuck process,
so these run it against real child programs and a fake server, and pin what it stops for,
what it never writes down, and what never reaches a child."""
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fcntl")

from canopy_runner.local_apps import host as host_mod  # noqa: E402
from canopy_runner.local_apps import server as server_mod  # noqa: E402
from canopy_runner.local_apps import state  # noqa: E402

LOCAL_APPS = Path(__file__).resolve().parents[3] / "local_apps"

FAST = dict(kill_after=0.4, heartbeat_every=0.05, unreachable_after=30.0, ended_grace=5.0,
            tick=0.02, exit_drain=0.2)

# A tiny protocol helper every fake program starts with.
PRELUDE = """
import json, sys, time, os
def out(m):
    sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
def lines():
    for line in sys.stdin:
        yield json.loads(line)
"""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


class FakeLink:
    beats = True

    def __init__(self, *, beat=None, canopy=None):
        self.beat, self.canopy_answer = beat, canopy
        self.beats_seen, self.calls, self.finished = [], [], []

    def heartbeat(self, run_state, counts):
        self.beats_seen.append({"state": run_state, "counts": counts})
        if self.beat:
            return self.beat()
        return {"stop": False, "reason": ""}

    def canopy(self, op, args):
        self.calls.append((op, args))
        return self.canopy_answer(op, args) if self.canopy_answer else (200, {"ok": True})

    def finish(self, reason, counts):
        self.finished.append((reason, counts))


def program(tmp_path, name, body):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / "main.py").write_text(PRELUDE + body)
    return folder


def child(folder, key="x-app", settings=None):
    return host_mod.ChildSpec(key=key, version="1.0", entry=["{python}", "main.py"], cwd=str(folder),
                              settings=settings or {})


def make_spec(app_folder, *, source_folder=None, canopy=(), sensitive=False, max_minutes=5, settings=None):
    return host_mod.RunSpec(
        key="x-app", version="1.0", run_id="run-1", max_minutes=max_minutes, canopy=list(canopy),
        sensitive=sensitive, unpublished=False, app=child(app_folder, settings=settings),
        source=child(source_folder, key="x-source") if source_folder else None)


def run(spec, link=None, **limits):
    host = host_mod.Host(spec, link or FakeLink(), host_mod.Limits(**{**FAST, **limits}))
    reason = host.run()
    return host, reason


def run_in_background(spec, link=None, **limits):
    host = host_mod.Host(spec, link or FakeLink(), host_mod.Limits(**{**FAST, **limits}))
    box = {}
    thread = threading.Thread(target=lambda: box.update(reason=host.run()), daemon=True)
    thread.start()
    return host, thread, box


def wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return False


def run_record():
    return json.loads(state.run_file("x-app").read_text())


def log_text():
    return state.log_file("x-app").read_text()


LISTEN_FOREVER = """
for m in lines():
    if m["type"] == "stop":
        out({"type": "stopped", "reason": "bye"})
        break
"""


def test_the_app_gets_start_and_its_status_and_events_are_recorded(tmp_path):
    seen = tmp_path / "seen.json"
    app = program(tmp_path, "app", f"""
m = next(lines())
open({str(seen)!r}, "w").write(json.dumps(m))
out({{"type": "status", "state": "listening", "counts": {{"sent": 4, "words": 812}}}})
out({{"type": "event", "name": "sent", "fields": {{"words": 120}}}})
out({{"type": "stopped", "reason": "all_done"}})
""")
    link = FakeLink()
    _, reason = run(make_spec(app, settings={"brief": "b"}), link)
    assert json.loads(seen.read_text()) == {"type": "start", "settings": {"brief": "b"}, "run_id": "run-1"}
    assert reason == "all_done"
    record = run_record()
    assert record["stop_reason"] == "all_done" and record["stopped_at"]
    assert record["counts"] == {"sent": 4, "words": 812}
    assert record["last_event"] == "sent"
    assert link.finished == [("all_done", {"sent": 4, "words": 812})]


def test_a_declared_call_is_forwarded_and_an_undeclared_one_never_reaches_the_server(tmp_path):
    results = tmp_path / "results.json"
    app = program(tmp_path, "app", f"""
next(lines())
out({{"type": "canopy.call", "id": "1", "op": "sessions.create", "args": {{"title": "t"}}}})
out({{"type": "canopy.call", "id": "2", "op": "turns.status", "args": {{"turn_id": "x"}}}})
got = []
for m in lines():
    got.append(m)
    if len(got) == 2:
        break
open({str(results)!r}, "w").write(json.dumps(got))
""")
    link = FakeLink(canopy=lambda op, args: (201, {"id": "s1", "url": "https://c.test/s1"}))
    run(make_spec(app, canopy=["sessions.create"]), link)
    got = {m["id"]: m for m in json.loads(results.read_text())}
    assert got["1"]["status"] == 201 and got["1"]["body"]["id"] == "s1"
    assert got["2"]["status"] == 403
    assert link.calls == [("sessions.create", {"title": "t"})]


def test_stream_items_reach_the_app_in_order(tmp_path):
    got = tmp_path / "got.json"
    source = program(tmp_path, "source", """
next(lines())
out({"type": "segment", "start": 1, "end": 2, "speaker": "S", "text": "one"})
out({"type": "segment", "start": 2, "end": 3, "speaker": "S", "text": "two"})
out({"type": "ended"})
""")
    app = program(tmp_path, "app", f"""
got = []
for m in lines():
    got.append(m)
    if m["type"] == "ended":
        break
open({str(got)!r}, "w").write(json.dumps(got))
out({{"type": "stopped", "reason": "meeting_ended"}})
""")
    _, reason = run(make_spec(app, source_folder=source))
    items = json.loads(got.read_text())
    assert [m["type"] for m in items] == ["start", "segment", "segment", "ended"]
    assert [m["text"] for m in items[1:3]] == ["one", "two"]
    assert reason == "meeting_ended"


def test_gone_reaches_the_app_before_stop(tmp_path):
    got = tmp_path / "got.json"
    source = program(tmp_path, "source", """
next(lines())
out({"type": "segment", "start": 1, "end": 2, "speaker": "S", "text": "one"})
out({"type": "gone"})
for m in lines():
    pass
""")
    app = program(tmp_path, "app", f"""
got = []
for m in lines():
    got.append(m["type"])
    if m["type"] == "stop":
        break
open({str(got)!r}, "w").write(json.dumps(got))
""")
    _, reason = run(make_spec(app, source_folder=source))
    assert json.loads(got.read_text()) == ["start", "segment", "gone", "stop"]
    assert reason == host_mod.SOURCE_GONE


def test_a_source_that_dies_counts_as_gone_so_the_app_drops_what_it_holds(tmp_path):
    got = tmp_path / "got.json"
    source = program(tmp_path, "source", "next(lines())\nos._exit(3)\n")
    app = program(tmp_path, "app", f"""
got = []
for m in lines():
    got.append(m["type"])
    if m["type"] == "stop":
        break
open({str(got)!r}, "w").write(json.dumps(got))
""")
    _, reason = run(make_spec(app, source_folder=source))
    assert json.loads(got.read_text()) == ["start", "gone", "stop"]
    assert reason == host_mod.SOURCE_EXITED


def test_a_source_that_refuses_gives_the_run_its_reason(tmp_path):
    source = program(tmp_path, "source", 'next(lines())\nout({"type": "stopped", "reason": "stale_meeting"})\n')
    app = program(tmp_path, "app", LISTEN_FOREVER)
    _, reason = run(make_spec(app, source_folder=source))
    assert reason == "stale_meeting"
    assert run_record()["stop_reason"] == "stale_meeting"


def test_when_the_app_exits_the_source_is_stopped_and_a_crash_says_so(tmp_path):
    marker = tmp_path / "source-stopped"
    source = program(tmp_path, "source", f"""
for m in lines():
    if m["type"] == "stop":
        open({str(marker)!r}, "w").write("yes")
        break
""")
    app = program(tmp_path, "app", "next(lines())\nos._exit(9)\n")
    _, reason = run(make_spec(app, source_folder=source))
    assert reason == host_mod.APP_CRASHED
    assert marker.exists()


def test_a_clean_exit_with_no_reason_is_app_exited(tmp_path):
    _, reason = run(make_spec(program(tmp_path, "app", "next(lines())\n")))
    assert reason == host_mod.APP_EXITED


def test_a_source_can_end_while_the_app_finishes_its_last_words(tmp_path):
    source = program(tmp_path, "source", 'next(lines())\nout({"type": "ended"})\n')
    app = program(tmp_path, "app", """
for m in lines():
    if m["type"] == "ended":
        time.sleep(0.3)
        out({"type": "stopped", "reason": "meeting_ended"})
        break
""")
    _, reason = run(make_spec(app, source_folder=source))
    assert reason == "meeting_ended"


def test_an_app_that_lingers_after_ended_is_stopped_at_the_end_of_the_grace(tmp_path):
    source = program(tmp_path, "source", 'next(lines())\nout({"type": "ended"})\n')
    _, reason = run(make_spec(program(tmp_path, "app", LISTEN_FOREVER), source_folder=source), ended_grace=0.2)
    assert reason == host_mod.SOURCE_ENDED


def test_an_app_that_ignores_stop_is_killed_along_with_what_it_started(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    app = program(tmp_path, "app", f"""
import signal, subprocess
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open({str(pid_file)!r}, "w").write(str(p.pid))
out({{"type": "status", "state": "stubborn", "counts": {{}}}})
while True:
    time.sleep(1)
""")
    host, thread, box = run_in_background(make_spec(app))
    assert wait_until(lambda: host.run_state == "stubborn")
    started = time.monotonic()
    pid = host.app.proc.pid
    host.request_stop()
    thread.join(10)
    assert box["reason"] == host_mod.STOPPED_BY_YOU
    assert 0.3 <= time.monotonic() - started < 5
    assert run_record()["killed"] == ["app"]
    assert host.app.proc.returncode == -9
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    grandchild = int(pid_file.read_text())

    def dead():
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return True
        return False
    assert wait_until(dead)


def test_the_time_cap_stops_the_app(tmp_path):
    spec = make_spec(program(tmp_path, "app", LISTEN_FOREVER), max_minutes=0.5 / 60)
    started = time.monotonic()
    _, reason = run(spec)
    assert reason == host_mod.MAX_MINUTES
    assert time.monotonic() - started < 5


def test_the_server_can_stop_a_run_and_its_reason_is_recorded(tmp_path):
    link = FakeLink(beat=lambda: {"stop": True, "reason": "revoked: bad version"})
    _, reason = run(make_spec(program(tmp_path, "app", LISTEN_FOREVER)), link)
    assert reason == "revoked: bad version"
    assert run_record()["stop_reason"] == "revoked: bad version"
    assert link.finished[0][0] == "revoked: bad version"


def _raise(error):
    def beat():
        raise error
    return beat


def test_a_removed_app_or_gone_run_stops_at_once(tmp_path):
    link = FakeLink(beat=_raise(server_mod.RunGone("404")))
    _, reason = run(make_spec(program(tmp_path, "app", LISTEN_FOREVER)), link, unreachable_after=60)
    assert reason == host_mod.REMOVED


def test_being_unable_to_reach_the_server_stops_only_after_the_window(tmp_path):
    link = FakeLink(beat=_raise(server_mod.Unreachable("503")))
    started = time.monotonic()
    _, reason = run(make_spec(program(tmp_path, "app", LISTEN_FOREVER)), link, unreachable_after=0.6)
    assert reason == host_mod.UNREACHABLE
    assert time.monotonic() - started >= 0.6
    assert len(link.beats_seen) > 1


def test_a_reachable_server_keeps_the_clock_from_running_out(tmp_path):
    spec = make_spec(program(tmp_path, "app", LISTEN_FOREVER))
    host, thread, box = run_in_background(spec, unreachable_after=0.5)
    time.sleep(1.0)
    assert "reason" not in box
    host.request_stop()
    thread.join(10)
    assert box["reason"] == host_mod.STOPPED_BY_YOU


def test_a_410_from_a_canopy_call_stops_the_run_with_the_reason_it_gave(tmp_path):
    app = program(tmp_path, "app", """
next(lines())
out({"type": "canopy.call", "id": "1", "op": "sessions.send", "args": {}})
for m in lines():
    if m["type"] == "stop":
        break
""")
    link = FakeLink(canopy=lambda op, args: (410, {"reason": "version revoked"}))
    _, reason = run(make_spec(app, canopy=["sessions.send"]), link)
    assert reason == "version revoked"


def test_a_call_made_while_stopping_is_refused_not_forwarded(tmp_path):
    results = tmp_path / "r.json"
    app = program(tmp_path, "app", f"""
for m in lines():
    if m["type"] == "stop":
        out({{"type": "canopy.call", "id": "late", "op": "sessions.send", "args": {{}}}})
    if m["type"] == "canopy.result":
        open({str(results)!r}, "w").write(json.dumps(m))
        break
""")
    link = FakeLink()
    host, thread, _ = run_in_background(make_spec(app, canopy=["sessions.send"]), link, kill_after=3)
    assert wait_until(lambda: host.app is not None)
    host.request_stop()
    thread.join(10)
    assert json.loads(results.read_text())["status"] == 409
    assert link.calls == []


def test_bad_lines_are_counted_and_never_crash_or_get_logged(tmp_path):
    app = program(tmp_path, "app", """
next(lines())
for bad in ("not json with secretword", "[1, 2]", '{"no": "type"}', '{"type": 3}', '{"type": "mystery"}',
            '{"type": "segment", "text": "secretword"}', '{"type": "canopy.call"}', "\\xff"):
    sys.stdout.write(bad + "\\n")
sys.stdout.write("x" * 5000 + "\\n")
sys.stdout.flush()
out({"type": "stopped", "reason": "survived"})
""")
    host, reason = run(make_spec(app), max_line=1000)
    assert reason == "survived"
    assert host.bad_lines == 9
    assert run_record()["counts"]["bad_lines"] == 9
    assert "secretword" not in log_text()


def test_a_child_gets_only_the_scrubbed_environment(tmp_path, monkeypatch):
    for name in ("CANOPY_WEB_PAT", "WORKBENCH_TOKEN", "AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    dumps = {name: tmp_path / f"{name}.json" for name in ("app", "source")}
    body = 'next(lines())\nopen({path!r}, "w").write(json.dumps(dict(os.environ)))\n'
    source = program(tmp_path, "source", body.format(path=str(dumps["source"])) + "for m in lines():\n    pass\n")
    app = program(tmp_path, "app", body.format(path=str(dumps["app"])))
    run(make_spec(app, source_folder=source))
    for path in dumps.values():
        env = json.loads(path.read_text())
        assert "secret-value" not in env.values()
        # macOS's Python may add these itself on startup; they carry no secret.
        assert set(env) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE", "__PYVENV_LAUNCHER__"} \
            <= {"PATH", "HOME", "LANG", "TMPDIR"}


def test_the_scrubbed_environment_has_a_path_even_without_one():
    assert state.scrubbed_env({"SECRET": "x"}) == {"PATH": state.DEFAULT_PATH}


SPOKEN = "the-confidential-salary-figure"


def _chatty_run(tmp_path, *, sensitive):
    source = program(tmp_path, "source", f"""
next(lines())
print({SPOKEN!r}, file=sys.stderr, flush=True)
out({{"type": "segment", "start": 1, "end": 2, "speaker": "S", "text": {SPOKEN!r}}})
out({{"type": "ended"}})
""")
    app = program(tmp_path, "app", f"""
for m in lines():
    if m["type"] == "ended":
        break
print({SPOKEN!r}, file=sys.stderr, flush=True)
words = {SPOKEN!r} + " said out loud in the meeting"
out({{"type": "status", "state": words, "counts": {{"sent": 1, words: 9, "leak": words}}}})
out({{"type": "event", "name": words, "fields": {{"text": words, "nested": {{"text": words}}, "words": 3}}}})
out({{"type": "event", "name": "session", "fields": {{"session_url": "https://c.test/s/1"}}}})
out({{"type": "event", "name": "bad", "fields": {{"session_url": words}}}})
time.sleep(0.2)
out({{"type": "stopped", "reason": words}})
""")
    link = FakeLink()
    host, reason = run(make_spec(app, source_folder=source, sensitive=sensitive), link)
    return host, reason, link


def test_spoken_words_never_reach_the_log_the_run_record_or_a_heartbeat(tmp_path):
    host, reason, link = _chatty_run(tmp_path, sensitive=True)
    assert host.relayed == 2
    assert SPOKEN not in log_text()
    assert SPOKEN not in state.run_file("x-app").read_text()
    assert SPOKEN not in json.dumps(link.beats_seen) and SPOKEN not in json.dumps(link.finished)
    assert SPOKEN not in reason
    for beat in link.beats_seen:
        assert all(isinstance(v, (int, float)) for v in beat["counts"].values())


def test_a_long_field_becomes_its_length_and_only_session_url_is_exempt(tmp_path):
    _chatty_run(tmp_path, sensitive=True)
    assert run_record()["session_url"] == "https://c.test/s/1"
    events = [json.loads(line) for line in log_text().splitlines()]
    fields = [e["fields"] for e in events if e["kind"] == "event"]
    words = SPOKEN + " said out loud in the meeting"
    assert fields[0]["text"] == len(words) and fields[0]["words"] == 3
    assert isinstance(fields[0]["nested"], int)
    assert fields[1] == {"session_url": "https://c.test/s/1"}
    assert fields[2] == {"session_url": len(words)}


def test_stderr_goes_to_the_log_truncated_only_when_the_run_is_not_sensitive(tmp_path):
    app = program(tmp_path, "app", """
next(lines())
print("x" * 2000, file=sys.stderr, flush=True)
time.sleep(0.2)
""")
    run(make_spec(app, sensitive=False))
    lines = [json.loads(line) for line in log_text().splitlines()]
    stderr = [e for e in lines if e["kind"] == "stderr"]
    assert len(stderr) == 1 and len(stderr[0]["text"]) == host_mod.STDERR_KEEP


def test_clean_fields_and_counts_keep_only_numbers_and_short_identifiers():
    assert host_mod.clean_fields({"a": 1, "b": "ok-id_1", "c": "two words", "d": None, "bad key": 1,
                                  "session_url": "https://x.test/1"}) == {
        "a": 1, "b": "ok-id_1", "c": 9, "d": None, "session_url": "https://x.test/1"}
    assert host_mod.clean_fields({"other_url": "https://x.test/1"}) == {"other_url": 16}
    assert host_mod.clean_fields("nope") == {}
    assert host_mod.clean_counts({"a": 1, "b": 2.5, "c": "3", "d": True, "bad key": 1}) == {"a": 1, "b": 2.5}


def test_a_second_host_for_the_same_app_refuses_and_finishes_its_run(tmp_path):
    host, thread, _ = run_in_background(make_spec(program(tmp_path, "app", LISTEN_FOREVER)))
    assert wait_until(lambda: host.app is not None)
    second_link = FakeLink()
    second = host_mod.Host(make_spec(tmp_path / "app"), second_link, host_mod.Limits(**FAST))
    second.lock.acquire = lambda wait=0: False
    assert second.run() == "already_running"
    assert second_link.finished[0][0] == "already_running"
    host.request_stop()
    thread.join(10)


def test_a_program_that_will_not_start_is_a_recorded_stop(tmp_path):
    spec = make_spec(tmp_path)
    spec.app.entry = [str(tmp_path / "does-not-exist")]
    _, reason = run(spec)
    assert reason == "app_failed_to_start"
    assert run_record()["stop_reason"] == "app_failed_to_start"


def test_the_stub_writes_what_would_have_been_sent_privately_and_answers(tmp_path):
    stub = server_mod.StubLink("x-app")
    assert stub.beats is False
    status, body = stub.canopy("sessions.create", {"title": "t"})
    assert status == 201 and body["id"] and body["url"]
    assert stub.canopy("sessions.send", {"text": "words"})[1]["turn_id"]
    assert stub.canopy("turns.status", {"turn_id": "x"})[1]["status"] == "done"
    path = state.would_send_file("x-app")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert '"words"' in path.read_text()


def test_a_stub_run_sends_no_heartbeats_and_cannot_run_out_of_server(tmp_path):
    class Stub(server_mod.StubLink):
        def heartbeat(self, run_state, counts):
            raise AssertionError("a dev run must not heartbeat")

    spec = make_spec(program(tmp_path, "app", 'next(lines())\ntime.sleep(0.5)\n'))
    _, reason = run(spec, Stub("x-app"), unreachable_after=0.1)
    assert reason == host_mod.APP_EXITED


def _fake_superwhisper(tmp_path, monkeypatch):
    """Live on the first two reads, finished after that."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "reads"
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    seg = {"text": SPOKEN, "start": 1, "end": 2, "speaker": 0}
    bye = {"text": "goodbye", "start": 3, "end": 4, "speaker": 0}
    live = {"segments": [seg], "speakers": [], "summary": "", "transcript": ""}
    over = {"segments": [seg, bye], "speakers": [], "summary": "s", "transcript": ""}
    path = bin_dir / "superwhisper"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        "if 'read' not in sys.argv:\n"
        f"    print({json.dumps([{'id': 'abc', 'datetime': now}])!r}); sys.exit(0)\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"print({json.dumps(live)!r} if n < 2 else {json.dumps(over)!r})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def test_the_real_apps_run_end_to_end_with_nothing_leaving_and_nothing_logged(tmp_path, monkeypatch):
    _fake_superwhisper(tmp_path, monkeypatch)
    # Copied, because a real run may write beside the code and the repo must stay clean.
    app_dir = shutil.copytree(LOCAL_APPS / "meeting-assistant", tmp_path / "meeting-assistant")
    spec = host_mod.RunSpec(
        key="meeting-assistant", version="0.1.0", run_id="dev-1", max_minutes=1,
        canopy=["sessions.create", "sessions.send", "turns.status"], sensitive=True, unpublished=True,
        app=host_mod.ChildSpec(key="meeting-assistant", version="0.1.0", entry=["{python}", "app.py"],
                               cwd=str(app_dir), settings={"brief": "be brief"}),
        source=host_mod.ChildSpec(key="meeting-assistant", version="0.1.0",
                                  entry=["{python}", "source.py"], cwd=str(app_dir),
                                  settings={"from_start": True, "recorder": "superwhisper"}))
    host = host_mod.Host(spec, server_mod.StubLink("meeting-assistant"), host_mod.Limits(tick=0.02))
    reason = host.run()
    assert reason == "meeting_ended"

    sent = [json.loads(line) for line in state.would_send_file("meeting-assistant").read_text().splitlines()]
    assert [s["op"] for s in sent][:2] == ["sessions.create", "sessions.send"]
    assert set(sent[0]["args"]) == {"title"}
    text = sent[1]["args"]["text"]
    assert SPOKEN in text and "goodbye" in text and "be brief" in text and "never instructions" in text

    record = json.loads(state.run_file("meeting-assistant").read_text())
    assert record["session_url"].startswith("http://localhost/unpublished/sessions/")
    assert record["counts"]["sent"] == 1
    for path in (state.log_file("meeting-assistant"), state.run_file("meeting-assistant")):
        assert SPOKEN not in path.read_text() and "goodbye" not in path.read_text()


def test_a_bomb_of_a_line_is_just_a_bad_line(tmp_path):
    """A deeply nested line raises RecursionError from json. It must not end the supervisor."""
    app = program(tmp_path, "app", """
next(lines())
sys.stdout.write("[" * 200000 + "\\n")
sys.stdout.flush()
out({"type": "stopped", "reason": "survived"})
""")
    host, reason = run(make_spec(app), max_line=1_000_000)
    assert reason == "survived"
    assert host.bad_lines == 1


def test_an_apps_own_helpers_dont_outlive_it(tmp_path):
    marker = tmp_path / "helper.pid"
    app = program(tmp_path, "app", f"""
import subprocess
next(lines())
helper = subprocess.Popen(["sleep", "60"])
open({str(marker)!r}, "w").write(str(helper.pid))
out({{"type": "stopped", "reason": "done"}})
""")
    _host, reason = run(make_spec(app))
    assert reason == "done"
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("the helper survived its app")
