"""`canopy-runner app …` against a fake server: what a download must prove, what start
refuses, that stopping needs no network, and that the wire shapes are the agreed ones."""
import argparse
import base64
import http.server
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fcntl")

from canopy_runner import main as main_mod  # noqa: E402
from canopy_runner.client import Client  # noqa: E402
from canopy_runner.local_apps import commands, package, server, state  # noqa: E402

KEY = "my-app"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(commands, "current_platform", lambda: "macos")
    return tmp_path / "home"


def parse(*argv):
    return main_mod._build_parser().parse_args(["app", "--config", "/nonexistent/runner.json", *argv])


def write_app(folder: Path, *, key=KEY, version="0.1.0", body="", **manifest):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({
        "key": key, "name": key, "version": version, "entry": ["{python}", "app.py"],
        "platforms": ["macos"], "max_minutes": 60, **manifest}))
    (folder / "app.py").write_text(body or "import sys\nfor line in sys.stdin:\n    if 'stop' in line:\n        break\n")
    return folder


def listing(folder: Path, **changes) -> tuple[dict, bytes]:
    manifest, packed = package.pack(folder)
    entry = {"key": manifest["key"], "name": manifest["name"], "version": manifest["version"],
             "sha256": package.sha256(packed), "size": len(packed), "notice": manifest["notice"],
             "disclosure": manifest["disclosure"], "entry": manifest["entry"],
             "max_minutes": manifest["max_minutes"], "settings": {}, "needs_acceptance": False, "changes": [],
             "refusal": None, "running": False, "source_key": None, "consumes": manifest["consumes"],
             "provides": manifest["provides"], "checks": manifest["checks"], **changes}
    return entry, packed


class FakeServer:
    def __init__(self, *apps):
        self.entries = [entry for entry, _ in apps]
        self.packages = {entry["key"]: packed for entry, packed in apps}
        self.started, self.finished, self.downloads = [], [], []
        self.start_refusal = None

    def list_apps(self):
        return self.entries

    def package(self, key, version):
        self.downloads.append((key, version))
        return self.packages[key]

    def start_run(self, key, version):
        if self.start_refusal:
            raise server.Refused(self.start_refusal)
        self.started.append((key, version))
        return "run-1"

    def finish(self, run_id, reason, counts):
        self.finished.append((run_id, reason))


@pytest.fixture
def no_spawn(monkeypatch):
    """Stands in for the detached supervisor, which a test of `start` itself doesn't need."""
    spawned = []

    def fake(spec):
        spawned.append(spec)
        state.ensure_dir(spec.key)
        state.write_json(state.run_file(spec.key), {"run_id": spec.run_id})

        class Proc:
            def poll(self):
                return None
        return Proc()
    monkeypatch.setattr(commands, "spawn_host", fake)
    return spawned


def use(monkeypatch, fake):
    monkeypatch.setattr(commands, "_server", lambda args: fake)


def test_the_parser_accepts_what_the_menu_bar_sends():
    args = main_mod._build_parser().parse_args(["app", "--config", "/c.json", "list", "--json"])
    assert (args.command, args.app_command, args.config, args.json) == ("app", "list", "/c.json", True)
    assert parse("stop", "--all").all is True
    assert parse("start", KEY, "--accept-notice").accept_notice is True


def test_the_daemon_path_never_imports_app_code():
    code = ("import sys; import canopy_runner.main as m; m._build_parser();"
            "bad = [n for n in sys.modules if 'local_apps' in n]; sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0


def test_start_downloads_checks_and_starts_with_the_notice_accepted(tmp_path, monkeypatch, no_spawn):
    entry, packed = listing(write_app(tmp_path / "a", notice="Tell them.", canopy=["sessions.create"]))
    entry["settings"] = {"brief": "b"}
    fake = FakeServer((entry, packed))
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    assert fake.started == [(KEY, "0.1.0")]
    spec = no_spawn[0]
    assert spec.run_id == "run-1" and spec.unpublished is False and spec.source is None
    assert spec.canopy == ["sessions.create"] and spec.app.settings == {"brief": "b"}
    assert spec.app.cwd == str(state.version_dir(KEY, "0.1.0"))
    assert stat.S_IMODE((Path(spec.app.cwd) / "app.py").stat().st_mode) == 0o444


def test_a_second_start_uses_what_is_on_disk(tmp_path, monkeypatch, no_spawn):
    fake = FakeServer(listing(write_app(tmp_path / "a")))
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    assert len(fake.downloads) == 1


@pytest.mark.parametrize("change,word", [({"sha256": "0" * 64}, "sha256"), ({"size": 3}, "bytes")])
def test_a_download_that_does_not_match_is_refused_and_nothing_runs(tmp_path, monkeypatch, no_spawn, capsys, change, word):
    entry, packed = listing(write_app(tmp_path / "a"), **change)
    fake = FakeServer((entry, packed))
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert word in capsys.readouterr().out
    assert not state.version_dir(KEY, "0.1.0").exists()
    assert no_spawn == [] and fake.started == []


def test_a_package_naming_another_app_or_a_traversing_key_is_refused(tmp_path, monkeypatch, no_spawn, capsys):
    entry, packed = listing(write_app(tmp_path / "other", key="other-app"), key=KEY)
    use(monkeypatch, FakeServer((entry, packed)))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "says it is" in capsys.readouterr().out and no_spawn == []

    entry, packed = listing(write_app(tmp_path / "a"), version="../../evil")
    use(monkeypatch, FakeServer((entry, packed)))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "not a version" in capsys.readouterr().out and no_spawn == []


def test_a_listed_refusal_is_shown_and_nothing_is_downloaded(tmp_path, monkeypatch, no_spawn, capsys):
    entry, packed = listing(write_app(tmp_path / "a"), refusal="This version was revoked.")
    fake = FakeServer((entry, packed))
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "revoked" in capsys.readouterr().out
    assert fake.downloads == [] and no_spawn == []


def test_the_servers_refusal_of_the_run_is_shown(tmp_path, monkeypatch, no_spawn, capsys):
    fake = FakeServer(listing(write_app(tmp_path / "a")))
    fake.start_refusal = "Local apps are switched off in this workspace."
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "switched off" in capsys.readouterr().out and no_spawn == []


def test_without_acceptance_and_without_a_tty_start_refuses_and_shows_the_notice(tmp_path, monkeypatch, no_spawn, capsys):
    fake = FakeServer(listing(write_app(tmp_path / "a", notice="Tell the people in it.",
                                        disclosure={"reads": ["the words"], "sends": ["the words"]})))
    use(monkeypatch, fake)
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert commands.run(parse("start", KEY)) == 1
    out = capsys.readouterr().out
    assert "Tell the people in it." in out and "--accept-notice" in out
    assert fake.started == [] and no_spawn == []


class Tty:
    def isatty(self):
        return True


def test_on_a_tty_a_person_confirms_the_notice(tmp_path, monkeypatch, no_spawn, capsys):
    use(monkeypatch, FakeServer(listing(write_app(tmp_path / "a", notice="Tell them."))))
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert commands.run(parse("start", KEY)) == 1
    assert no_spawn == []
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert commands.run(parse("start", KEY)) == 0
    assert len(no_spawn) == 1


def test_a_failed_check_stops_the_start_and_says_how_to_fix_it(tmp_path, monkeypatch, no_spawn, capsys):
    checks = [{"label": "Tool is set up", "run": ["{python}", "-c", "raise SystemExit(1)"], "fix": "Install it."},
              {"label": "Other is fine", "run": ["{python}", "-c", "pass"], "fix": "n/a"}]
    fake = FakeServer(listing(write_app(tmp_path / "a", checks=checks)))
    use(monkeypatch, fake)
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    out = capsys.readouterr().out
    assert "Tool is set up" in out and "Install it." in out and "Other is fine" not in out
    assert fake.started == [] and no_spawn == []

    assert commands.run(parse("check", KEY, "--json")) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is False
    assert [(c["label"], c["ok"], c["fix"]) for c in answer["checks"]] == [
        ("Tool is set up", False, "Install it."), ("Other is fine", True, "n/a")]


def test_a_check_that_hangs_fails_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "CHECK_SECONDS", 0.2)
    check = {"label": "x", "run": ["{python}", "-c", "import time; time.sleep(30)"], "fix": "y"}
    started = time.monotonic()
    assert commands._run_check(check, tmp_path) is False
    assert time.monotonic() - started < 10


def test_a_check_gets_the_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_PAT", "secret-value")
    check = {"label": "x", "run": ["{python}", "-c", "import os; raise SystemExit(1 if 'CANOPY_WEB_PAT' in os.environ else 0)"],
             "fix": "y"}
    assert commands._run_check(check, tmp_path) is True


def test_an_app_with_a_source_starts_both_and_is_sensitive(tmp_path, monkeypatch, no_spawn):
    source_entry, source_packed = listing(write_app(tmp_path / "s", key="the-source", provides=["transcript"]))
    source_entry["settings"] = {"from_start": True}
    entry, packed = listing(write_app(tmp_path / "a", consumes=["transcript"]), source_key="the-source")
    use(monkeypatch, FakeServer((entry, packed), (source_entry, source_packed)))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    spec = no_spawn[0]
    assert spec.sensitive is True
    assert spec.source.key == "the-source" and spec.source.settings == {"from_start": True}


def test_a_consumer_with_no_source_and_a_source_alone_are_refused(tmp_path, monkeypatch, no_spawn, capsys):
    entry, packed = listing(write_app(tmp_path / "a", consumes=["transcript"]))
    source_entry, source_packed = listing(write_app(tmp_path / "s", key="the-source", provides=["transcript"]))
    use(monkeypatch, FakeServer((entry, packed), (source_entry, source_packed)))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "needs a source" in capsys.readouterr().out
    assert commands.run(parse("start", "the-source", "--accept-notice")) == 1
    assert "is a source" in capsys.readouterr().out
    assert no_spawn == []


def test_another_platform_is_refused(tmp_path, monkeypatch, no_spawn, capsys):
    monkeypatch.setattr(commands, "current_platform", lambda: "linux")
    use(monkeypatch, FakeServer(listing(write_app(tmp_path / "a"))))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "doesn't run on linux" in capsys.readouterr().out and no_spawn == []


def test_a_pending_acceptance_is_said_and_stays_on_the_accepted_version(tmp_path, monkeypatch, no_spawn, capsys):
    entry, packed = listing(write_app(tmp_path / "a"), needs_acceptance=True, changes=["Now also reads your calendar."])
    use(monkeypatch, FakeServer((entry, packed)))
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    out = capsys.readouterr().out
    assert "Now also reads your calendar." in out and len(no_spawn) == 1


def test_list_merges_what_is_here_and_survives_no_network(tmp_path, monkeypatch, capsys):
    write_app(tmp_path / "dev", key="dev-app", notice="n")
    assert commands.run(parse("dev-install", str(tmp_path / "dev"))) == 0
    capsys.readouterr()
    entry, _ = listing(write_app(tmp_path / "a"))
    use(monkeypatch, FakeServer((entry, b"")))
    assert commands.run(parse("list", "--json")) == 0
    answer = json.loads(capsys.readouterr().out)
    by_key = {a["key"]: a for a in answer["apps"]}
    assert by_key[KEY]["unpublished"] is False and by_key["dev-app"]["unpublished"] is True
    assert answer["offline"] is False

    def down(args):
        raise server.Unreachable("no route")
    monkeypatch.setattr(commands, "_server", down)
    assert commands.run(parse("list", "--json")) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["offline"] is True and [a["key"] for a in answer["apps"]] == ["dev-app"]


def test_dev_install_records_the_folder_and_makes_no_server_call(tmp_path, monkeypatch, no_spawn, capsys):
    def no_server(args):
        raise AssertionError("a dev run must not reach a server")
    monkeypatch.setattr(commands, "_server", no_server)
    folder = write_app(tmp_path / "a", settings={"brief": {"type": "longtext"}, "agent": {"type": "agent", "required": True}})
    assert commands.run(parse("dev-install", str(folder), "--set", "brief=hello")) == 0
    assert "unpublished" in capsys.readouterr().out
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    spec = no_spawn[0]
    assert spec.unpublished is True and spec.run_id.startswith("dev-")
    assert spec.app.cwd == str(folder.resolve()) and spec.app.settings == {"brief": "hello"}


def test_dev_install_refuses_a_bad_manifest_or_setting(tmp_path, capsys):
    folder = write_app(tmp_path / "a", surprise=1)
    assert commands.run(parse("dev-install", str(folder))) == 1
    assert "surprise" in capsys.readouterr().out
    folder = write_app(tmp_path / "b")
    assert commands.run(parse("dev-install", str(folder), "--set", "nope=1")) == 1
    assert "no setting nope" in capsys.readouterr().out


def test_a_dev_consumer_uses_a_dev_source_and_never_a_published_one(tmp_path, no_spawn, capsys):
    write_app(tmp_path / "a", consumes=["transcript"])
    assert commands.run(parse("dev-install", str(tmp_path / "a"))) == 0
    capsys.readouterr()
    assert commands.run(parse("start", KEY, "--accept-notice")) == 1
    assert "dev-install one first" in capsys.readouterr().out
    write_app(tmp_path / "s", key="the-source", provides=["transcript"])
    assert commands.run(parse("dev-install", str(tmp_path / "s"))) == 0
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    assert no_spawn[0].source.key == "the-source" and no_spawn[0].sensitive is True


def test_pack_writes_the_same_bytes_each_time(tmp_path, capsys):
    folder = write_app(tmp_path / "a")
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    assert commands.run(parse("pack", str(folder), "--out", str(one))) == 0
    assert commands.run(parse("pack", str(folder), "--out", str(two))) == 0
    assert one.read_bytes() == two.read_bytes()
    assert package.sha256(one.read_bytes()) in capsys.readouterr().out


def test_remove_refuses_a_running_app_and_otherwise_clears_it(tmp_path, capsys):
    folder = write_app(tmp_path / "a")
    assert commands.run(parse("dev-install", str(folder))) == 0
    lock = state.HostLock(KEY)
    assert lock.acquire()
    assert commands.run(parse("remove", KEY)) == 1
    lock.release()
    assert commands.run(parse("remove", KEY)) == 0
    assert not state.app_dir(KEY).exists()
    assert (folder / "app.py").exists()


def _start_detached(tmp_path, capsys, *, body=""):
    folder = write_app(tmp_path / "a", notice="Tell them.", body=body)
    assert commands.run(parse("dev-install", str(folder))) == 0
    assert commands.run(parse("start", KEY, "--accept-notice")) == 0
    capsys.readouterr()


def test_a_real_detached_run_shows_in_status_and_stops_with_no_network(tmp_path, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("stop and status must not use the network")
    _start_detached(tmp_path, capsys)
    monkeypatch.setattr(commands, "_server", no_network)
    monkeypatch.setattr("socket.socket.connect", no_network)

    assert commands.run(parse("status", "--json")) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["running"] == 1
    row = answer["apps"][0]
    assert row["key"] == KEY and row["running"] and row["unpublished"] and row["pid"] != os.getpid()
    assert stat.S_IMODE(state.run_file(KEY).stat().st_mode) == 0o600
    assert not state.spec_file(KEY).exists()

    assert commands.run(parse("stop", "--all")) == 0
    assert f"{KEY}: stopped" in capsys.readouterr().out
    assert commands.run(parse("status", KEY, "--json")) == 0
    row = json.loads(capsys.readouterr().out)["apps"][0]
    assert row["running"] is False and row["stop_reason"] == "stopped_by_you"


def test_stop_all_with_nothing_running_and_a_run_that_cannot_be_stopped_politely(tmp_path, capsys, monkeypatch):
    assert commands.run(parse("stop", "--all")) == 0
    assert "Nothing was running" in capsys.readouterr().out
    # A host that won't answer SIGTERM (wedged) still has to go.
    state.ensure_dir(KEY)
    code = ("import signal, sys, time; from canopy_runner.local_apps import state;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"lock = state.HostLock({KEY!r}); lock.acquire(); print('ready', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    state.write_json(state.run_file(KEY), {"pid": proc.pid, "run_id": "r", "children": []})
    outcome = commands.stop_apps([KEY], wait=0.3)
    assert outcome == {KEY: "killed"}
    assert proc.wait(timeout=10) == -9
    record = state.read_json(state.run_file(KEY))
    assert record["stop_reason"] == commands.KILLED_BY_STOP


def test_status_says_when_a_host_died_without_recording_a_stop(tmp_path):
    state.ensure_dir(KEY)
    state.write_json(state.run_file(KEY), {"pid": 999999, "run_id": "r", "stopped_at": None, "state": "listening"})
    row = state.describe(KEY)
    assert row["running"] is False and row["stop_reason"] == state.HOST_DIED and row["pid"] is None


def test_start_hands_over_no_pipe_so_a_caller_reading_to_the_end_does_not_hang(tmp_path):
    """The menu bar reads this command's stdout to EOF; a host holding it would hang for the whole run."""
    folder = write_app(tmp_path / "a")
    env = dict(os.environ)
    code = ("import sys; from canopy_runner import main as m; from canopy_runner.local_apps import commands as c;"
            "c.current_platform = lambda: 'macos';"
            f"sys.argv = ['canopy-runner', 'app', 'dev-install', {str(folder)!r}]; "
            "\ntry:\n    m.main()\nexcept SystemExit:\n    pass\n"
            "sys.argv = ['canopy-runner', 'app', 'start', 'my-app', '--accept-notice']; m.main()")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stdout
    assert "started" in done.stdout
    assert commands.stop_apps([KEY]) == {KEY: "stopped"}


# --- the wire ------------------------------------------------------------------------


class Recorder(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Recorder.seen.append({"method": self.command, "path": self.path,
                              "auth": self.headers.get("Authorization"),
                              "body": json.loads(body) if body else None})
        status, payload = Recorder.routes.get(self.path, (404, {"detail": "nope"}))
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _answer


@pytest.fixture
def wire():
    Recorder.routes, Recorder.seen = {}, []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


PREFIX = "/api/harness/runners/box-1/local-apps"


def _server_on(url):
    return server.RunnerServer(Client(url, "runner-token"), "box-1")


def test_the_run_routes_and_their_shapes(wire):
    srv = _server_on(wire)
    Recorder.routes = {
        f"{PREFIX}/": (200, {"apps": [{"key": KEY}]}),
        f"{PREFIX}/{KEY}/package?version=1.0": (200, b"zip-bytes"),
        f"{PREFIX}/{KEY}/runs/": (201, {"run_id": "r1"}),
        f"{PREFIX}/runs/r1/heartbeat": (200, {"stop": True, "reason": "revoked"}),
        f"{PREFIX}/runs/r1/finish": (200, {}),
        f"{PREFIX}/runs/r1/canopy": (200, {"status": 201, "body": {"id": "s1", "url": "u"}}),
    }
    assert srv.list_apps() == [{"key": KEY}]
    assert srv.package(KEY, "1.0") == b"zip-bytes"
    assert srv.start_run(KEY, "1.0") == "r1"
    assert srv.heartbeat("r1", "listening", {"sent": 1}) == {"stop": True, "reason": "revoked"}
    srv.finish("r1", "max_minutes", {"sent": 1})
    assert srv.canopy("r1", "sessions.create", {"title": "t"}) == (201, {"id": "s1", "url": "u"})
    by_path = {s["path"]: s for s in Recorder.seen}
    assert by_path[f"{PREFIX}/{KEY}/runs/"]["body"] == {"version": "1.0"}
    assert by_path[f"{PREFIX}/runs/r1/heartbeat"]["body"] == {"state": "listening", "counts": {"sent": 1}}
    assert by_path[f"{PREFIX}/runs/r1/finish"]["body"] == {"reason": "max_minutes", "counts": {"sent": 1}}
    assert by_path[f"{PREFIX}/runs/r1/canopy"]["body"] == {"op": "sessions.create", "args": {"title": "t"}}
    assert all(s["auth"] == "Bearer runner-token" for s in Recorder.seen)


@pytest.mark.parametrize("status", [403, 409])
def test_a_start_refusal_carries_the_servers_words(wire, status):
    Recorder.routes = {f"{PREFIX}/{KEY}/runs/": (status, {"detail": "This version was revoked."})}
    with pytest.raises(server.Refused, match="This version was revoked."):
        _server_on(wire).start_run(KEY, "1.0")


@pytest.mark.parametrize("status", [404, 410])
def test_a_heartbeat_answering_gone_says_so(wire, status):
    Recorder.routes = {f"{PREFIX}/runs/r1/heartbeat": (status, {"detail": "gone"})}
    with pytest.raises(server.RunGone):
        _server_on(wire).heartbeat("r1", "listening", {})


@pytest.mark.parametrize("status", [401, 500, 503])
def test_a_heartbeat_failing_otherwise_is_only_unreachable(wire, status):
    Recorder.routes = {f"{PREFIX}/runs/r1/heartbeat": (status, {"detail": "x"})}
    with pytest.raises(server.Unreachable):
        _server_on(wire).heartbeat("r1", "listening", {})


def test_a_canopy_call_refused_at_the_door_is_an_answer_not_an_exception(wire):
    Recorder.routes = {f"{PREFIX}/runs/r1/canopy": (410, {"detail": "version revoked"})}
    assert _server_on(wire).canopy("r1", "sessions.send", {}) == (410, {"detail": "version revoked"})
    assert server.RunnerServer(Client("http://127.0.0.1:9", "t"), "box-1").canopy("r1", "x", {})[0] == server.UNREACHABLE


def test_submit_uses_the_persons_token_and_never_the_runners(tmp_path, wire, monkeypatch, capsys):
    folder = write_app(tmp_path / "a")
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"base_url": wire, "token": "runner-token", "runner_id": "box-1"}))
    answer = {"id": "1", "key": KEY, "version": "0.1.0", "sha256": "abc", "status": "pending",
              "approval_id": "2", "review_url": "https://c.test/review/1"}
    Recorder.routes = {"/api/local-apps/submissions/": (201, answer)}
    monkeypatch.setenv("CANOPY_WEB_PAT", "person-token")
    args = argparse.Namespace(app_command="submit", folder=str(folder), note="first", config=str(config))
    assert commands.run(args) == 0
    seen = Recorder.seen[0]
    assert seen["auth"] == "Bearer person-token"
    assert seen["body"]["note"] == "first"
    assert base64.b64decode(seen["body"]["package_b64"]) == package.pack(folder)[1]
    assert "https://c.test/review/1" in capsys.readouterr().out


def test_submit_refuses_clearly_without_a_token(tmp_path, wire, monkeypatch, capsys):
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"base_url": wire, "token": "runner-token", "runner_id": "box-1"}))
    args = argparse.Namespace(app_command="submit", folder=str(write_app(tmp_path / "a")), note="", config=str(config))
    assert commands.run(args) == 1
    assert "CANOPY_WEB_PAT" in capsys.readouterr().out
    assert Recorder.seen == []


def test_submit_shows_the_servers_refusal(tmp_path, wire, monkeypatch, capsys):
    Recorder.routes = {"/api/local-apps/submissions/": (409, {"detail": "That version exists with different bytes."})}
    with pytest.raises(server.Refused, match="different bytes"):
        server.submit(wire, "person-token", b"zip", "")
