"""The root job starter: what it will do, for whom, and the exact unit it asks for."""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import time

import pytest
from _job_broker_harness import Harness, load_broker

T = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
GOOD_START = {"op": "start", "turn": T, "slug": "eva", "run": ["python3", "x.py"], "timeout_seconds": 900}


@pytest.fixture
def jb():
    return load_broker()


# ── requests ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    {"op": "status"}, {"op": "cleanup"}, {"op": "stop", "turn": T}, GOOD_START,
    {"op": "export", "turn": T, "size": 10},
    {"op": "write_token", "turn": T, "provider": "salesforce", "size": 10},
    {"op": "write_token", "turn": T, "provider": "salesforce", "size": 0},
    {**GOOD_START, "run": ["./sync.sh", "; rm -rf /", "$(id)", "a=b"]},
])
def test_good_requests_pass(jb, req):
    assert jb.validate_request(req) == req


@pytest.mark.parametrize("req", [
    {}, [], "status", None, {"op": 3},
    {"op": "exec"}, {"op": "shell", "command": "id"}, {"op": "start"}, {"op": "STATUS"},
], ids=str)
def test_unknown_operation_refused(jb, req):
    with pytest.raises(jb.Refused):
        jb.validate_request(req)


@pytest.mark.parametrize("turn", ["", "../x", "a/b", "x" * 36, "turn-1", T + "\n", T.replace("1", "A"),
                                  T.replace("-", ""), 3, None, [T]], ids=repr)
def test_bad_turn_id_refused(jb, turn):
    for req in ({**GOOD_START, "turn": turn}, {"op": "stop", "turn": turn},
                {"op": "export", "turn": turn, "size": 1}):
        with pytest.raises(jb.Refused):
            jb.validate_request(req)


@pytest.mark.parametrize("extra", [
    {"path": "/etc/passwd"}, {"dest": "/home/cu-x/.canopy"}, {"cwd": "/"}, {"root": "/"}, {"dir": "../.."},
], ids=str)
def test_a_path_is_never_taken_from_a_request(jb, extra):
    for req in (GOOD_START, {"op": "export", "turn": T, "size": 1}):
        with pytest.raises(jb.Refused):
            jb.validate_request({**req, **extra})


@pytest.mark.parametrize("provider", ["../../etc/passwd", "a/b", "..", "A", "", "x" * 41, "a.json", "a\n", 3])
def test_a_provider_that_would_leave_the_job_root_refused(jb, provider):
    with pytest.raises(jb.Refused):
        jb.validate_request({"op": "write_token", "turn": T, "provider": provider, "size": 1})


@pytest.mark.parametrize("extra", [{"user": "root"}, {"user": "cu-dimagi"}, {"uid": 0}, {"gid": 0}, {"group": "sudo"},
                                   {"env": {"X": "1"}}, {"properties": ["NoNewPrivileges=no"]}], ids=str)
def test_a_user_or_a_property_is_never_taken_from_a_request(jb, extra):
    with pytest.raises(jb.Refused):
        jb.validate_request({**GOOD_START, **extra})


@pytest.mark.parametrize("run", [
    "make all", None, [], {}, [3], ["make", 3], [""], ["make", "a\0b"], [["make"]],
    ["/bin/sh"], ["-c"], ["a/../b"], ["LD_PRELOAD=x", "make"], ["x"] * 300, ["y" * 200000],
], ids=lambda r: repr(r)[:30])
def test_argv_that_isnt_a_list_of_strings_refused(jb, run):
    with pytest.raises(jb.Refused):
        jb.validate_request({**GOOD_START, "run": run})


@pytest.mark.parametrize("value", [0, -1, 7201, True, 1.5, "60", None], ids=repr)
def test_bad_timeout_refused(jb, value):
    with pytest.raises(jb.Refused):
        jb.validate_request({**GOOD_START, "timeout_seconds": value})


@pytest.mark.parametrize("slug", ["a b", "a=b", "a\nb", "A", "$x", "%i", "x" * 65, 3, None], ids=repr)
def test_bad_slug_refused(jb, slug):
    with pytest.raises(jb.Refused):
        jb.validate_request({**GOOD_START, "slug": slug})


@pytest.mark.parametrize("req", [
    {"op": "export", "turn": T, "size": 0}, {"op": "export", "turn": T, "size": -1},
    {"op": "export", "turn": T, "size": 10 ** 12}, {"op": "export", "turn": T, "size": True},
    {"op": "write_token", "turn": T, "provider": "x", "size": 10 ** 7},
], ids=str)
def test_bad_size_refused(jb, req):
    with pytest.raises(jb.Refused):
        jb.validate_request(req)


# ── the unit ─────────────────────────────────────────────────────────────────

def _argv(jb, run=("python3", "x.py"), timeout=900):
    return jb.systemd_run_argv(T, "eva", list(run), timeout, root="/var/lib/canopy-jobs")


JOB_DIR = f"/var/lib/canopy-jobs/{T}"


@pytest.mark.parametrize("prop", [
    "NoNewPrivileges=yes", "ProtectHome=yes", "ProtectSystem=strict", f"ReadWritePaths={JOB_DIR}", "PrivateTmp=yes",
    "PrivateDevices=yes", "ProtectProc=invisible", "ProcSubset=pid", "CapabilityBoundingSet=",
    "RestrictAddressFamilies=AF_INET AF_INET6", "IPAddressDeny=169.254.169.254 fd00:ec2::254 localhost",
    "IPAddressAllow=127.0.0.53", "KillMode=control-group", "RuntimeMaxSec=900", "MemoryMax=2G", "MemorySwapMax=0",
    "CPUQuota=100%", "TasksMax=256", "LimitFSIZE=5G", "TimeoutStopSec=10s", f"WorkingDirectory={JOB_DIR}/repo",
    "InaccessiblePaths=-/dev/shm", "InaccessiblePaths=-/dev/mqueue", "InaccessiblePaths=-/opt/agents",
    "InaccessiblePaths=-/opt/canopy-runner", "InaccessiblePaths=-/opt/canopy-web",
    "InaccessiblePaths=-/etc/canopy-runner", "InaccessiblePaths=-/run/canopy", "InaccessiblePaths=-/run/canopy-kms",
    "InaccessiblePaths=-/run/canopy-aws", "InaccessiblePaths=-/run/canopy-jobs",
    f"Environment=PATH={'/usr/local/bin:/usr/bin:/bin'}", f"Environment=HOME={JOB_DIR}/home",
    "Environment=LANG=C.UTF-8", "Environment=CANOPY_AGENT_SLUG=eva", f"Environment=CANOPY_TURN_ID={T}",
    f"Environment=CANOPY_JOB_TOKENS={JOB_DIR}/tokens",
])
def test_unit_property_pinned(jb, prop):
    assert f"--property={prop}" in _argv(jb)


@pytest.mark.parametrize("flag", ["--wait", "--pipe", "--collect", "--quiet", "--expand-environment=no",
                                  "--service-type=exec", f"--unit=canopy-job-{T}", "--uid=canopy-job",
                                  "--gid=canopy-job"])
def test_unit_flag_pinned(jb, flag):
    assert flag in _argv(jb)[:_argv(jb).index("--")]


def test_the_environment_property_list_is_exactly_the_six(jb):
    names = [a.removeprefix("--property=Environment=").split("=")[0]
             for a in _argv(jb) if a.startswith("--property=Environment=")]
    assert sorted(names) == sorted(jb.SIX)


def test_argv_goes_after_the_separator_with_no_shell(jb):
    evil = "; rm -rf /"
    argv = _argv(jb, run=("./sync.sh", evil, "$HOME"))
    assert argv[0] == "systemd-run"
    tail = argv[argv.index("--") + 1:]
    assert tail[:2] == ["/usr/bin/env", "-i"]            # systemd's own USER, SHELL and the rest go too
    assert [w.split("=")[0] for w in tail[2:8]] == list(jb.SIX)
    assert tail[8:] == ["./sync.sh", evil, "$HOME"]
    assert not {"sh", "/bin/sh", "bash", "-c"} & set(tail)


def test_unpack_runs_with_no_owner_or_mode_from_the_archive(jb):
    assert jb.unpack_argv(f"{JOB_DIR}/repo") == ["tar", "-x", "--no-same-owner", "--no-same-permissions",
                                                 "-C", f"{JOB_DIR}/repo"]


def test_as_job_drops_to_the_job_user_and_nothing_else(jb, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(jb.subprocess, "run", run)
    jb.as_job_user(["true"])
    assert seen["user"] == "canopy-job" and seen["group"] == "canopy-job" and seen["extra_groups"] == []
    assert "CANOPY_TOKEN" not in seen["env"] and set(seen["env"]) == {"PATH"} and "shell" not in seen


@pytest.mark.parametrize("rc,elapsed,want", [(0, 100, False), (0, 900, False), (3, 100, False), (1, 900, True),
                                             (1, 950, True)])
def test_exit_reads_as_a_timeout_only_at_the_limit(jb, rc, elapsed, want):
    assert jb.exit_info(rc, elapsed, 900) == {"rc": rc, "timed_out": want}


@pytest.mark.parametrize("state,box,want", [
    ({}, {}, False), ({"applied": {"a": False, "b": False}}, {"RUNNER_EGRESS": ""}, False),
    ({"applied": {"a": False, "b": True}}, {}, True), ({}, {"RUNNER_EGRESS": "locked"}, True),
    ({}, {"RUNNER_DATA_MODE": "sensitive_data"}, True),
])
def test_any_locked_runner_locks_the_box(jb, state, box, want):
    assert jb.box_locked(state, box) is want


@pytest.mark.parametrize("text,want", [("systemd 255 (255.4-1ubuntu8)\n+PAM", 255), ("systemd 249\n", 249), ("", 0),
                                       ("nope", 0)])
def test_systemd_version_read(jb, text, want):
    assert jb.systemd_version(text) == want


def test_a_job_cannot_forge_its_exit_frame(jb):
    forged = b"X" + struct.pack(">I", 11) + b'{"rc": 0}\n\n'
    frame = jb.frame(b"O", forged)
    assert frame[:1] == b"O" and struct.unpack(">I", frame[1:5])[0] == len(forged) and frame[5:] == forged


# ── over the socket ──────────────────────────────────────────────────────────

@pytest.fixture
def box(jb, tmp_path):
    h = Harness(jb, tmp_path, f"MARK{tmp_path.name}")
    yield h
    h.close()


def _ask(h, req, body=b""):
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(20)
        c.connect(h.path)
        c.sendall(json.dumps(req).encode() + b"\n" + body)
        buf = b""
        while b"\n" not in buf:
            got = c.recv(65536)
            if not got:
                break
            buf += got
    return json.loads(buf)


def _frames(c):
    out, buf = [], b""
    while True:
        while len(buf) >= 5 and len(buf) >= 5 + struct.unpack(">I", buf[1:5])[0]:
            n = struct.unpack(">I", buf[1:5])[0]
            out.append((buf[:1], buf[5:5 + n]))
            buf = buf[5 + n:]
            if out[-1][0] == b"X":
                return out
        got = c.recv(65536)
        if not got:
            return out
        buf += got


def _tar(tmp_path, **files):
    import io
    import tarfile
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(text)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(text.encode()))
    return b.getvalue()


def _start(h, run, turn=T, timeout=60):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(30)
    c.connect(h.path)
    c.sendall(json.dumps({"op": "start", "turn": turn, "slug": "eva", "run": run,
                          "timeout_seconds": timeout}).encode() + b"\n")
    return c


def test_a_peer_with_another_uid_is_refused_before_anything(box):
    box.uid = 999
    for req in ({"op": "status"}, {"op": "cleanup"}, GOOD_START, {"op": "not-even-an-op"}):
        assert "not a runner" in _ask(box, req)["error"]
    assert box.requests == [] and box.as_job_calls == [] and box.spawned == []


def test_root_is_not_a_runner_either(box):
    box.uid = 0
    assert "not a runner" in _ask(box, {"op": "status"})["error"]


def test_an_unknown_operation_is_refused_over_the_socket(box):
    assert "error" in _ask(box, {"op": "exec", "command": "id"})
    assert box.requests == []


def test_a_request_that_is_not_json_is_refused(box):
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(10)
        c.connect(box.path)
        c.sendall(b"not json\n")
        assert b"error" in c.recv(4096)


def test_status_answers(box):
    got = _ask(box, {"op": "status"})
    assert got == {"ok": True, "systemd": 255, "supported": True, "job_user": True, "locked": False, "running": ""}


def test_export_tokens_and_a_run_end_to_end(box, tmp_path):
    data = _tar(tmp_path, **{"x.txt": "from the archive\n"})
    assert _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)["ok"]
    assert (tmp_path / T / "repo" / "x.txt").read_text() == "from the archive\n"
    assert (tmp_path / T / "home").is_dir() and (tmp_path / T / "tokens").is_dir()
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")["ok"]
    assert (tmp_path / T / "tokens" / "sf.json").read_text() == "abc"
    assert oct((tmp_path / T / "tokens" / "sf.json").stat().st_mode)[-3:] == "600"
    with _start(box, ["cat", "x.txt"]) as c:
        frames = _frames(c)
    assert (b"O", b"from the archive\n") in frames
    assert json.loads(frames[-1][1]) == {"rc": 0, "timed_out": False}
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 0})["ok"]
    assert not (tmp_path / T / "tokens" / "sf.json").exists()
    assert _ask(box, {"op": "cleanup"})["ok"]
    assert list(tmp_path.glob("*-*-*-*-*")) == []


def test_start_without_an_export_is_refused(box):
    with _start(box, ["true"]) as c:
        frames = _frames(c)
    assert "error" in json.loads(frames[-1][1]) and box.spawned == []


def test_token_for_another_turn_is_refused(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)
    assert "error" in _ask(box, {"op": "write_token", "turn": T2, "provider": "sf", "size": 1}, b"x")


def test_a_locked_box_starts_nothing(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)
    box.locked = True
    with _start(box, ["true"]) as c:
        frames = _frames(c)
    assert "locked" in json.loads(frames[-1][1])["error"] and box.spawned == []


def test_one_job_at_a_time_and_another_runner_cant_touch_it(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)
    py = ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker]
    box.mod.JOB_PATH = os.environ["PATH"]
    with _start(box, py) as c:
        c.settimeout(20)
        assert c.recv(4096)[5:] == b"up\n"
        box.runners.add(2002)
        box.uid = 2002
        for req, body in (({"op": "export", "turn": T2, "size": len(data)}, data),
                          ({"op": "stop", "turn": T}, b""), ({"op": "cleanup"}, b"")):
            assert "error" in _ask(box, req, body), req
        assert box.stops == []
        box.uid = 2001
        assert _ask(box, {"op": "stop", "turn": T})["ok"] and box.stops == [T]
        frames = _frames(c)
    assert "rc" in json.loads(frames[-1][1])


def test_a_runner_going_away_stops_its_job(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)
    box.mod.JOB_PATH = os.environ["PATH"]
    c = _start(box, ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker])
    assert c.recv(4096)[5:] == b"up\n"
    c.close()
    for _ in range(200):
        if box.stops:
            break
        time.sleep(0.05)
    assert box.stops == [T]


def test_cleanup_stops_what_its_own_caller_left(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _ask(box, {"op": "export", "turn": T, "size": len(data)}, data)
    box.mod.JOB_PATH = os.environ["PATH"]
    with _start(box, ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker]) as c:
        assert c.recv(4096)[5:] == b"up\n"
        assert _ask(box, {"op": "cleanup"})["ok"]
    assert T in box.stops and list(tmp_path.glob("*-*-*-*-*")) == []


def test_install_restarts_only_when_its_code_changed(jb, tmp_path):
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    unit = tmp_path / "canopy-job-broker.service"
    assert jb.install(run=run, unit_path=unit) is True
    text = unit.read_text()
    assert "CANOPY_JOB_BROKER_SHA=" in text and "User=" not in text
    assert ["systemctl", "restart", "canopy-job-broker.service"] in calls
    calls.clear()
    assert jb.install(run=run, unit_path=unit) is False
    assert ["systemctl", "restart", "canopy-job-broker.service"] not in calls


def test_the_broker_stays_stdlib_only():
    import ast
    import pathlib
    import sys
    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "job_broker.py").read_text())
    names = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names)
