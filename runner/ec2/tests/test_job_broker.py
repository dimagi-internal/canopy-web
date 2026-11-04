"""The root job starter: what it will do, for whom, and the exact unit it asks for."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
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
    "NoNewPrivileges=yes", "ProtectHome=yes", "ProtectSystem=strict", "PrivateTmp=yes",
    "TemporaryFileSystem=/var/lib/canopy-jobs", f"BindPaths={JOB_DIR}",
    "SystemCallArchitectures=native", "RestrictNamespaces=yes", "PrivateIPC=yes", "RemoveIPC=yes",
    "LockPersonality=yes",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes", "ProtectControlGroups=yes",
    "ProtectClock=yes", "ProtectHostname=yes", "RestrictSUIDSGID=yes", "RestrictRealtime=yes",
    "PrivateDevices=yes", "ProtectProc=invisible", "ProcSubset=pid", "CapabilityBoundingSet=",
    "RestrictAddressFamilies=AF_INET AF_INET6",
    "IPAddressDeny=169.254.169.254 fd00:ec2::254 localhost 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 "
    "100.64.0.0/10 fe80::/10 fc00::/7",
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


@pytest.mark.parametrize("absent", ["ReadWritePaths", "MemoryDenyWriteExecute"])
def test_unit_property_left_out_on_purpose(jb, absent):
    """ReadWritePaths would leave the sibling jobs in view; MemoryDenyWriteExecute breaks runtimes."""
    assert not [a for a in _argv(jb) if a.startswith(f"--property={absent}")]


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


def _props(argv):
    return [a for a in argv[:argv.index("--")] if a.startswith("--property=")]


#: What a helper may differ from its job in: how long, where it starts, and which six values.
_UNIT_SPECIFIC = ("--property=RuntimeMaxSec=", "--property=WorkingDirectory=", "--property=Environment=",
                  "--property=BindPaths=")


def test_a_helper_gets_every_property_a_job_gets(jb):
    shared = [p for p in _props(_argv(jb)) if not p.startswith(_UNIT_SPECIFIC)]
    assert len(shared) > 30
    helper = jb.helper_argv(T, ["true"], root="/var/lib/canopy-jobs", seq=1)
    assert set(shared) <= set(_props(helper))
    assert f"--property=BindPaths={JOB_DIR}" in helper and f"--property=WorkingDirectory={JOB_DIR}" in helper
    assert "--uid=canopy-job" in helper and "--gid=canopy-job" in helper


def test_helper_units_have_their_own_names(jb):
    one = jb.helper_argv(T, ["true"], root="/var/lib/canopy-jobs", seq=1)
    two = jb.helper_argv(T, ["true"], root="/var/lib/canopy-jobs", seq=2)
    assert f"--unit=canopy-job-helper-1-{T}" in one and f"--unit=canopy-job-helper-2-{T}" in two


def test_nothing_runs_as_the_job_user_outside_a_unit(jb):
    source = pathlib.Path(jb.__file__).read_text()
    assert "user=JOB_USER" not in source and "extra_groups" not in source and "as_job_user" not in source


def test_python_helpers_load_nothing_from_the_users_site(jb):
    assert jb.token_argv("/r", T, "sf.json")[:4] == ["python3", "-I", "-S", "-c"]


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
    h.holders = []
    yield h
    for c in h.holders:
        c.close()
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
    assert box.requests == [] and box.helper_units == [] and box.spawned == []


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
    assert got == {"ok": True, "systemd": 255, "supported": True, "job_user": True, "locked": False, "running": "",
                   "wedged": False}


def _hold(h, turn, data):
    """Export, and keep the connection: it holds the slot, as a runner does for a whole job."""
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(20)
    c.connect(h.path)
    with contextlib.suppress(OSError):   # a refused export closes without reading its body
        c.sendall(json.dumps({"op": "export", "turn": turn, "size": len(data)}).encode() + b"\n" + data)
    buf = b""
    while b"\n" not in buf:
        got = c.recv(65536)
        if not got:
            break
        buf += got
    h.holders.append(c)
    return c, json.loads(buf)


def _wait(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_export_tokens_and_a_run_end_to_end(box, tmp_path):
    data = _tar(tmp_path, **{"x.txt": "from the archive\n"})
    assert _hold(box, T, data)[1]["ok"]
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


def test_a_slot_starts_one_job(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    with _start(box, ["true"]) as c:
        assert json.loads(_frames(c)[-1][1])["rc"] == 0
    with _start(box, ["true"]) as c:
        assert "error" in json.loads(_frames(c)[-1][1])
    assert len(box.spawned) == 1


def test_token_for_another_turn_is_refused(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    assert "error" in _ask(box, {"op": "write_token", "turn": T2, "provider": "sf", "size": 1}, b"x")


def test_a_locked_box_starts_nothing(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    box.locked = True
    with _start(box, ["true"]) as c:
        frames = _frames(c)
    assert "locked" in json.loads(frames[-1][1])["error"] and box.spawned == []


# ── the slot: from export through cleanup (the review's critical) ───────────
# Two jobs share a uid, so one could read the other's tokens. Nobody else may start in between.

BUSY = "Another job is running on this machine."


def _as_b(box):
    box.runners.add(2002)
    box.uid = 2002


def test_another_runner_cant_export_between_a_token_and_the_start(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")["ok"]
    _as_b(box)
    assert _hold(box, T2, data)[1]["error"] == BUSY
    assert not (tmp_path / T2).exists() and (tmp_path / T / "tokens" / "sf.json").read_text() == "abc"


def test_another_runner_cant_export_between_a_jobs_exit_and_its_cleanup(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")["ok"]
    with _start(box, ["true"]) as c:
        assert json.loads(_frames(c)[-1][1])["rc"] == 0     # A's job has exited
    _as_b(box)
    assert _hold(box, T2, data)[1]["error"] == BUSY
    with _start(box, ["true"], turn=T2) as c:
        assert "error" in json.loads(_frames(c)[-1][1])
    assert len(box.spawned) == 1 and (tmp_path / T / "tokens" / "sf.json").exists()


def test_the_same_runner_cant_stack_a_second_slot_either(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    assert _hold(box, T2, data)[1]["error"] == BUSY


def test_a_holder_going_away_releases_the_slot_and_clears_its_files(box, tmp_path):
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")["ok"]
    c.close()
    assert _wait(lambda: not (tmp_path / T).exists())
    _as_b(box)
    assert _hold(box, T2, data)[1]["ok"] and (tmp_path / T2).is_dir() and not (tmp_path / T).exists()


def test_a_holder_going_away_stops_its_running_job_too(box, tmp_path):
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    box.mod.JOB_PATH = os.environ["PATH"]
    s = _start(box, ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker])
    assert s.recv(4096)[5:] == b"up\n"
    c.close()
    assert _wait(lambda: T in box.stops and not (tmp_path / T).exists())
    s.close()


def test_files_that_couldnt_be_cleared_refuse_the_next_export(box, tmp_path, monkeypatch):
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    real = box.run_unit
    monkeypatch.setattr(box.server, "run_unit",
                        lambda argv, input=None: subprocess.CompletedProcess(argv, 1, b"", b"") if "find" in argv
                        else real(argv, input=input))
    c.close()
    time.sleep(0.5)
    _as_b(box)
    got = _hold(box, T2, data)[1]
    assert "error" in got and not (tmp_path / T2).exists()
    monkeypatch.setattr(box.server, "run_unit", real)
    assert _hold(box, T2, data)[1]["ok"] and not (tmp_path / T).exists()


def test_a_stale_slot_times_out_with_the_jobs_maximum_and_is_cleared(jb, box, tmp_path, monkeypatch):
    assert jb.SLOT_MAX_SECONDS == 121 * 60
    monkeypatch.setattr(box.mod, "SLOT_MAX_SECONDS", 0.3)
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    assert _wait(lambda: not (tmp_path / T).exists())
    _as_b(box)
    assert _hold(box, T2, data)[1]["ok"]


def test_another_runners_cleanup_cant_wipe_a_prepared_job(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    _as_b(box)
    assert _ask(box, {"op": "cleanup"})["error"] == BUSY
    assert (tmp_path / T / "repo" / "a").exists()
    assert box.stops == []


def test_cleanup_failing_says_so(box, tmp_path, monkeypatch):
    _hold(box, T, _tar(tmp_path, a="1"))
    monkeypatch.setattr(box.server, "run_unit", lambda argv, input=None: subprocess.CompletedProcess(argv, 1, b"", b""))
    assert "error" in _ask(box, {"op": "cleanup"})


def test_one_job_at_a_time_and_another_runner_cant_touch_it(box, tmp_path):
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    py = ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker]
    box.mod.JOB_PATH = os.environ["PATH"]
    with _start(box, py) as c:
        c.settimeout(20)
        assert c.recv(4096)[5:] == b"up\n"
        _as_b(box)
        assert _hold(box, T2, data)[1]["error"] == BUSY
        for req in ({"op": "stop", "turn": T}, {"op": "cleanup"}):
            assert "error" in _ask(box, req), req
        assert box.stops == []
        box.uid = 2001
        assert _ask(box, {"op": "stop", "turn": T})["ok"] and T in box.stops
        frames = _frames(c)
    assert "rc" in json.loads(frames[-1][1])


def test_a_runner_going_away_stops_its_job(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    box.mod.JOB_PATH = os.environ["PATH"]
    c = _start(box, ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker])
    assert c.recv(4096)[5:] == b"up\n"
    c.close()
    assert _wait(lambda: T in box.stops)


def test_cleanup_stops_what_its_own_caller_left(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    box.mod.JOB_PATH = os.environ["PATH"]
    with _start(box, ["python3", "-c", "import time; print('up', flush=True); time.sleep(60)", box.marker]) as c:
        assert c.recv(4096)[5:] == b"up\n"
        assert _ask(box, {"op": "cleanup"})["ok"]
    assert T in box.stops and list(tmp_path.glob("*-*-*-*-*")) == []


def test_a_reader_that_stalls_doesnt_pin_the_slot(box, tmp_path, monkeypatch):
    monkeypatch.setattr(box.mod, "SEND_TIMEOUT_SECONDS", 0.5)
    _hold(box, T, _tar(tmp_path, a="1"))
    box.mod.JOB_PATH = os.environ["PATH"]
    loud = "import sys\nwhile True: sys.stdout.write('x' * 65536); sys.stdout.flush()\n"
    c = _start(box, ["python3", "-c", loud, box.marker])      # and never read
    assert _wait(lambda: T in box.stops, seconds=20)
    c.close()


# ── token files: a job can plant anything in its own folder ─────────────────

def _token(box, data=b"secret-token"):
    return _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": len(data)}, data)


def test_a_planted_tokens_link_cant_send_a_token_out(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    outside = tmp_path / "outside"
    outside.mkdir()
    tokens = tmp_path / T / "tokens"
    tokens.rmdir()
    tokens.symlink_to(outside)
    assert "error" in _token(box)
    assert list(outside.iterdir()) == []


def test_a_planted_job_dir_link_cant_send_a_token_out(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    outside = tmp_path / "outside" / "tokens"
    outside.mkdir(parents=True)
    job = tmp_path / T
    job.rename(tmp_path / "moved")
    job.symlink_to(tmp_path / "outside")
    assert "error" in _token(box)
    assert list(outside.iterdir()) == []
    job.unlink()
    (tmp_path / "moved").rename(job)


@pytest.mark.parametrize("planted", ["sf.json", "sf.json.tmp"])
def test_a_planted_file_link_is_replaced_not_followed(box, tmp_path, planted):
    _hold(box, T, _tar(tmp_path, a="1"))
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / T / "tokens" / planted).symlink_to(victim)
    assert _token(box)["ok"]
    assert victim.read_text() == "untouched"
    copy = tmp_path / T / "tokens" / "sf.json"
    assert not copy.is_symlink() and copy.read_text() == "secret-token"
    assert oct(copy.stat().st_mode)[-3:] == "600"
    assert not (tmp_path / T / "tokens" / "sf.json.tmp").exists()


def test_removing_a_planted_link_doesnt_follow_it(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / T / "tokens" / "sf.json").symlink_to(victim)
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 0})["ok"]
    assert victim.read_text() == "untouched" and not (tmp_path / T / "tokens" / "sf.json").is_symlink()


def test_a_refresh_replaces_the_token_atomically(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    assert _token(box, b"first-value")["ok"]
    inode = (tmp_path / T / "tokens" / "sf.json").stat().st_ino
    assert _token(box, b"second-value")["ok"]
    copy = tmp_path / T / "tokens" / "sf.json"
    assert copy.read_text() == "second-value" and copy.stat().st_ino != inode


# ── how much root holds ─────────────────────────────────────────────────────

def test_an_export_is_streamed_never_held_in_root(box, tmp_path, monkeypatch):
    writes = []
    real = box.run_unit_pipe

    def pipe(args):
        proc = real(args)
        stdin = proc.stdin

        class Counting:
            def write(self, data):
                writes.append(len(data))
                return stdin.write(data)

            def __getattr__(self, name):
                return getattr(stdin, name)
        proc.stdin = Counting()
        return proc
    monkeypatch.setattr(box.server, "run_unit_pipe", pipe)
    data = _tar(tmp_path, big="x" * (5 * 1024 * 1024))
    assert _hold(box, T, data)[1]["ok"]
    assert (tmp_path / T / "repo" / "big").stat().st_size == 5 * 1024 * 1024
    assert sum(writes) == len(data) and max(writes) <= box.mod.STREAM_CHUNK
    assert box.mod.STREAM_CHUNK <= 1024 * 1024


def test_what_root_can_hold_at_once_is_under_16_mb(jb):
    worst = jb.MAX_CONNECTIONS * (jb.MAX_HEAD + jb.MAX_TOKEN) + jb.STREAM_CHUNK
    assert worst <= 16 * 1024 * 1024


def test_a_refused_export_reads_no_body(box, tmp_path, monkeypatch):
    data = _tar(tmp_path, a="1")
    _hold(box, T, data)
    _as_b(box)
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5)
        c.connect(box.path)
        c.sendall(json.dumps({"op": "export", "turn": T2, "size": 10 ** 8}).encode() + b"\n")
        assert b"Another job" in c.recv(4096)       # with no body sent: it didn't wait for one


def test_an_export_cut_short_releases_its_slot(box, tmp_path):
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5)
        c.connect(box.path)
        c.sendall(json.dumps({"op": "export", "turn": T, "size": 1000}).encode() + b"\nshort")
    assert _wait(lambda: not (tmp_path / T).exists() and box.server._job is None)


# ── no process of the uid, before any slot is freed (the second critical) ──
# A helper or a daemon outside its unit could read the next job's tokens, so nothing is trusted
# to have ended: it is killed and confirmed gone, or the slot stays held.

def test_every_helper_of_a_slot_is_a_confined_unit(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")["ok"]
    assert _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 0})["ok"]
    assert _ask(box, {"op": "cleanup"})["ok"]
    words = [a for c in box.as_job_calls for a in c]
    assert "tar" in words and "find" in words and any("os.rename" in w for w in words)
    assert len(box.helper_units) >= 5
    for argv in box.helper_units:
        assert "--uid=canopy-job" in argv and f"--property=BindPaths={tmp_path}/{T}" in argv
        assert "--property=ProtectHome=yes" in argv and f"--property=TemporaryFileSystem={tmp_path}" in argv


def test_a_helper_that_cant_start_fails_closed(box, tmp_path, monkeypatch):
    def nope(argv, input=None):
        raise OSError("no systemd-run")
    _hold(box, T, _tar(tmp_path, a="1"))
    monkeypatch.setattr(box.server, "run_unit", nope)
    assert "error" in _ask(box, {"op": "write_token", "turn": T, "provider": "sf", "size": 3}, b"abc")
    assert not (tmp_path / T / "tokens" / "sf.json").exists()


def test_an_export_whose_helper_cant_start_fails_closed_and_frees_the_slot(box, tmp_path, monkeypatch):
    def nope(argv):
        raise OSError("no systemd-run")
    monkeypatch.setattr(box.server, "run_unit_pipe", nope)
    assert "error" in _hold(box, T, _tar(tmp_path, a="1"))[1]
    assert _wait(lambda: box.server._job is None) and not (tmp_path / T).exists()


def test_job_folders_are_made_by_root_and_given_to_the_job_user(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    assert len(box.chowned) == 4
    assert not [c for c in box.as_job_calls if c[0] == "mkdir"]


def test_a_slot_isnt_freed_until_no_process_of_the_uid_remains(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    box.pgrep_calls = 0
    assert _ask(box, {"op": "cleanup"})["ok"]
    assert box.pgrep_calls >= 1 and box.server._job is None


WEDGED = "couldn't be stopped"


def test_a_survivor_keeps_the_slot_and_says_so(box, tmp_path):
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    box.survivors = True
    got = _ask(box, {"op": "cleanup"})
    assert WEDGED in got["error"] and got["wedged"] is True
    assert box.server._job is not None and (tmp_path / T).exists()
    status = _ask(box, {"op": "status"})
    assert status["wedged"] is True


def test_closing_the_hold_doesnt_undo_a_refused_cleanup(box, tmp_path):
    """The runner closes its hold straight after cleanup, refused or not."""
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    box.survivors = True
    assert WEDGED in _ask(box, {"op": "cleanup"})["error"]
    c.close()
    time.sleep(0.5)
    assert box.server._job is not None and (tmp_path / T).exists()
    _as_b(box)
    got = _hold(box, T2, data)[1]
    assert WEDGED in got["error"] and got["wedged"] is True and not (tmp_path / T2).exists()


def test_a_slot_wedged_by_a_gone_holder_is_retried_by_the_next_export(box, tmp_path):
    data = _tar(tmp_path, a="1")
    c, _ = _hold(box, T, data)
    box.survivors = True
    c.close()
    assert _wait(lambda: box.server._job is not None and box.server._job.get("gone"))
    box.survivors = False
    _as_b(box)
    assert _hold(box, T2, data)[1]["ok"]
    assert not (tmp_path / T).exists() and _ask(box, {"op": "status"})["wedged"] is False


def test_a_slot_wedged_past_its_limit_stays_held(box, tmp_path, monkeypatch):
    monkeypatch.setattr(box.mod, "SLOT_MAX_SECONDS", 0.5)
    assert _hold(box, T, _tar(tmp_path, a="1"))[1]["ok"]
    box.survivors = True
    time.sleep(1.0)
    assert box.server._job is not None and (tmp_path / T).exists()


def test_a_process_left_over_refuses_the_next_export(box, tmp_path):
    box.survivors = True
    got = _hold(box, T, _tar(tmp_path, a="1"))[1]
    assert WEDGED in got["error"] and box.server._job is None and not (tmp_path / T).exists()


def test_a_unit_that_outlives_systemd_run_is_still_live(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    with _start(box, ["true"]) as c:
        assert json.loads(_frames(c)[-1][1])["rc"] == 0
    job = box.server._job
    assert box.server._live(job) is False
    box.unit_alive = True
    assert box.server._live(job) is True


def test_a_unit_that_wont_stop_keeps_the_slot(box, tmp_path, monkeypatch):
    monkeypatch.setattr(box.mod, "STOP_WAIT_SECONDS", 0.1)
    _hold(box, T, _tar(tmp_path, a="1"))
    with _start(box, ["true"]) as c:
        _frames(c)
    box.unit_alive = True
    assert WEDGED in _ask(box, {"op": "cleanup"})["error"]
    assert box.server._job is not None


def test_a_stop_that_fails_says_so(box, tmp_path):
    _hold(box, T, _tar(tmp_path, a="1"))
    box.stop_ok = False
    assert "error" in _ask(box, {"op": "stop", "turn": T})


def test_stop_unit_reads_systemctls_answer(jb, monkeypatch):
    for code, want in ((0, True), (5, True), (1, False)):      # 5: already gone, with --collect
        monkeypatch.setattr(jb.subprocess, "run",
                            lambda argv, code=code, **kw: subprocess.CompletedProcess(argv, code, "", ""))
        assert jb.stop_unit(T) is want


def test_an_unexpected_error_frees_the_slot_the_checked_way(box, tmp_path, monkeypatch):
    def boom(argv):
        raise ValueError("not a Refused")
    monkeypatch.setattr(box.server, "run_unit_pipe", boom)
    box.pgrep_calls = 0
    assert "error" in _hold(box, T, _tar(tmp_path, a="1"))[1]
    assert _wait(lambda: box.server._job is None) and box.pgrep_calls >= 1


def test_an_unexpected_error_with_a_survivor_keeps_the_slot(box, tmp_path, monkeypatch):
    def boom(argv):
        raise ValueError("not a Refused")
    real = box.no_processes
    calls = {"n": 0}

    def after_start():
        calls["n"] += 1
        return calls["n"] == 1 and real()       # clean for the export's own check, then a survivor
    monkeypatch.setattr(box.server, "no_processes", after_start)
    monkeypatch.setattr(box.server, "run_unit_pipe", boom)
    assert "error" in _hold(box, T, _tar(tmp_path, a="1"))[1]
    time.sleep(0.3)
    assert box.server._job is not None


def test_boot_sweep_stops_kills_and_clears(box, tmp_path):
    (tmp_path / T).mkdir()
    box.server.boot_sweep()
    assert box.sweeps == ["stop_all"] and box.pgrep_calls >= 1 and not (tmp_path / T).exists()
    assert box.server._wedged is None and box.server._dirty is False


def test_boot_sweep_with_a_survivor_wedges_the_box(box, tmp_path):
    (tmp_path / T).mkdir()
    box.survivors = True
    box.server.boot_sweep()
    assert box.server._wedged and (tmp_path / T).exists()
    assert _ask(box, {"op": "status"})["wedged"] is True
    assert WEDGED in _hold(box, T2, _tar(tmp_path, a="1"))[1]["error"]


def test_clear_copes_with_whatever_is_in_the_root(box, tmp_path):
    (tmp_path / T / "deep").mkdir(parents=True)
    (tmp_path / T / "deep" / "x").write_text("x")
    os.chmod(tmp_path / T / "deep", 0o500)
    (tmp_path / "a-file").write_text("x")
    (tmp_path / "a-link").symlink_to("/etc")
    (tmp_path / "sibling-check").mkdir()
    box.server.clear_files()
    assert list(tmp_path.iterdir()) == [] and box.server._dirty is False


def test_clear_never_follows_a_link(box, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (tmp_path / "a-link").symlink_to(outside)
    box.server.clear_files()
    assert (outside / "keep").exists() and list(tmp_path.iterdir()) == []


def test_clear_refuses_a_name_it_wont_put_in_a_property(box, tmp_path):
    (tmp_path / "a b").mkdir()
    with pytest.raises(box.mod.Refused):
        box.server.clear_files()
    assert box.server._dirty is True


def test_install_restarts_only_when_its_code_changed(jb, tmp_path):
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    unit = tmp_path / "canopy-job-broker.service"
    assert jb.install(run=run, unit_path=unit) is True
    text = unit.read_text()
    assert "CANOPY_JOB_BROKER_SHA=" in text and "User=" not in text
    assert ["systemctl", "restart", "--no-block", "canopy-job-broker.service"] in calls
    calls.clear()
    assert jb.install(run=run, unit_path=unit) is False
    assert ["systemctl", "restart", "--no-block", "canopy-job-broker.service"] not in calls


def test_install_never_waits_for_itself_at_boot(jb, tmp_path):
    """The unit is After=canopy-runners.service, the very oneshot `--setup-runners` runs in, so a
    blocking `enable --now` or `restart` there waits on its own caller."""
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    jb.install(run=run, unit_path=tmp_path / "u.service")
    starts = [c for c in calls if {"--now", "restart"} & set(c)]
    assert starts and all("--no-block" in c for c in starts)


def test_the_broker_stays_stdlib_only():
    import ast
    import sys
    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "job_broker.py").read_text())
    names = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names)
