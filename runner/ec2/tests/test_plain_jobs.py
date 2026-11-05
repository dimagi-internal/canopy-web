"""The box reads a plain job's definition, fetches its repo and runs it (Part 3, Tasks 2-3).

The one seam is the job broker's socket. `_job_broker_harness` runs the real broker on it,
with what it does as root stubbed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest
import yaml
from _job_broker_harness import Harness, load_broker

EC2 = pathlib.Path(__file__).resolve().parent.parent


T1, T2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", msg, cwd=repo)


@pytest.fixture
def origins(tmp_path):
    root = tmp_path / "github"
    repo = root / "tools"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("main v1\n")
    (repo / ".canopy").mkdir()
    (repo / ".canopy" / "jobs.yaml").write_text("version: 1\njobs:\n  sync: {run: [./sync.sh]}\n")
    _git("add", "-A", cwd=repo)
    _commit(repo, "v1")
    return root


@pytest.fixture
def runner(cloud_runner, monkeypatch, tmp_path, origins):
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "REPO_URL_TEMPLATE", str(origins / "{repo}"))
    monkeypatch.setattr(cloud_runner, "JOBS_ROOT", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(cloud_runner, "JOB_LOCK_FILE", tmp_path / "job.lock")
    (tmp_path / "job.lock").write_text("")
    cloud_runner._marker = f"MARK{tmp_path.name}"
    return cloud_runner


GOOD = "version: 1\njobs:\n  sync:\n    run: [./sync.sh, --fast]\n    timeout_minutes: 30\n"


# ── _job_spec ────────────────────────────────────────────────────────────────

def test_spec_reads_a_job(runner):
    spec = runner._job_spec(GOOD, "sync")
    assert spec.run == ["./sync.sh", "--fast"]
    assert spec.timeout_minutes == 30
    # bare name, default timeout
    spec = runner._job_spec("version: 1\njobs:\n  a-b: {run: [make]}\n", "a-b")
    assert spec.run == ["make"] and spec.timeout_minutes == 15


@pytest.mark.parametrize("text", [
    "version: 1\nextra: 1\njobs:\n  sync: {run: [make]}\n",
    "version: 1\njobs:\n  sync: {run: [make], shell: true}\n",
    "version: 2\njobs:\n  sync: {run: [make]}\n",
    "jobs:\n  sync: {run: [make]}\n",
])
def test_spec_unknown_key_refused(runner, text):
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec(text, "sync")
    assert str(exc.value).endswith(".")


@pytest.mark.parametrize("run", ["make all", "[]", "[make, 3]", "[./a/../b]", "[/bin/sh]",
                                 "['-rf']", "['']"])
def test_spec_string_run_refused(runner, run):
    with pytest.raises(runner.JobRefused):
        runner._job_spec(f"version: 1\njobs:\n  sync: {{run: {run}}}\n", "sync")


@pytest.mark.parametrize("minutes,ok", [(0, False), (1, True), (120, True), (121, False),
                                        (-5, False), ("10", False), (True, False), (1.5, False)])
def test_spec_timeout_bounds(runner, minutes, ok):
    text = f"version: 1\njobs:\n  sync: {{run: [make], timeout_minutes: {minutes!r}}}\n".replace("True", "true")
    if ok:
        assert runner._job_spec(text, "sync").timeout_minutes == minutes
    else:
        with pytest.raises(runner.JobRefused):
            runner._job_spec(text, "sync")


def test_spec_missing_name_sentence(runner):
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec(GOOD, "nightly")
    assert "nightly" in str(exc.value) and "sync" in str(exc.value)   # says what exists


def test_spec_not_yaml_sentence(runner):
    for text in ("a: [unclosed", "just a string", "- a\n- b\n", "", "\x00bad"):
        with pytest.raises(runner.JobRefused) as exc:
            runner._job_spec(text, "sync")
        assert "jobs.yaml" in str(exc.value)


def test_spec_over_64kb_refused(runner):
    big = GOOD + "# " + "x" * 70000 + "\n"
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec(big, "sync")
    assert "64" in str(exc.value)


def test_spec_aliases_are_bounded_by_the_size_cap(runner):
    # A small alias bomb parses to shared references, not copies, and stays under the cap.
    bomb = "version: 1\njobs:\n  sync: {run: [make]}\nx: &a [1,2,3]\n"
    with pytest.raises(runner.JobRefused):
        runner._job_spec(bomb, "sync")   # unknown key, however it was built
    assert len(bomb.encode()) < runner.JOBS_YAML_MAX_BYTES


@pytest.mark.parametrize("name", ["Sync; rm", "sync\n"])
def test_spec_bad_job_name_refused(runner, name):
    with pytest.raises(runner.JobRefused):
        runner._job_spec(GOOD, name)


@pytest.mark.parametrize("version", ["1.0", "true", "'1'"])
def test_spec_version_is_exactly_the_integer_one(runner, version):
    with pytest.raises(runner.JobRefused):
        runner._job_spec(f"version: {version}\njobs:\n  sync: {{run: [make]}}\n", "sync")


def test_spec_first_word_cannot_set_a_variable(runner):
    """`env` would read `LD_PRELOAD=x` as an assignment, past the six-variable rule."""
    with pytest.raises(runner.JobRefused):
        runner._job_spec("version: 1\njobs:\n  sync: {run: ['LD_PRELOAD=x', make]}\n", "sync")


def test_spec_read_from_the_archive(runner):
    data = runner._job_fetch_archive("tools", T1, None)
    assert runner._job_spec_from_archive(data, "sync").run == ["./sync.sh"]
    with pytest.raises(runner.JobRefused):
        runner._job_spec_from_archive(data, "absent")


def test_spec_missing_file_sentence(runner, origins):
    repo = origins / "tools"
    _git("rm", "-rq", ".canopy", cwd=repo)
    _commit(repo, "no jobs")
    data = runner._job_fetch_archive("tools", T2, None)
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec_from_archive(data, "sync")
    assert ".canopy/jobs.yaml" in str(exc.value)


def test_spec_that_is_a_link_refused(runner, origins):
    repo = origins / "tools"
    (repo / ".canopy" / "real.yaml").write_text(GOOD)
    (repo / ".canopy" / "jobs.yaml").unlink()
    (repo / ".canopy" / "jobs.yaml").symlink_to("real.yaml")
    _git("add", "-A", cwd=repo)
    _commit(repo, "link")
    data = runner._job_fetch_archive("tools", T1, None)
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec_from_archive(data, "sync")
    assert "link" in str(exc.value)


def _names(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return tar.getnames()


def _read(data, name):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return tar.extractfile(name).read().decode()


# ── fetching ─────────────────────────────────────────────────────────────────

def test_export_uses_main_not_a_branch(runner, origins):
    repo = origins / "tools"
    _git("checkout", "-q", "-b", "feature", cwd=repo)       # origin's HEAD is now feature
    (repo / "README.md").write_text("feature\n")
    _commit(repo, "feature")
    assert _read(runner._job_fetch_archive("tools", T1, None), "README.md") == "main v1\n"
    # and a stale clone is brought forward: main moves on, a new job sees it
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "README.md").write_text("main v2\n")
    _commit(repo, "v2")
    assert _read(runner._job_fetch_archive("tools", T2, None), "README.md") == "main v2\n"


def test_export_has_no_git_dir(runner):
    names = _names(runner._job_fetch_archive("tools", T1, None))
    assert ".canopy/jobs.yaml" in names
    assert not [n for n in names if n == ".git" or n.startswith(".git/")]


def test_export_leaves_no_ref_behind(runner):
    runner._job_fetch_archive("tools", T1, None)
    clone = runner._repos_root() / ".clones" / "tools.git"
    refs = _git("for-each-ref", "refs/jobs", cwd=clone)
    assert refs == ""


def test_failed_fetch_refuses_never_stale(runner, origins):
    runner._job_fetch_archive("tools", T1, None)               # clone exists now
    (origins / "tools").rename(origins / "gone")              # origin unreachable
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_fetch_archive("tools", T2, None)
    assert "latest" in str(exc.value)


def test_failed_first_clone_refuses(runner):
    with pytest.raises(runner.JobRefused):
        runner._job_fetch_archive("missing-repo", T1, None)


def test_missing_main_refuses(runner, origins):
    repo = origins / "tools"
    _git("branch", "-m", "main", "trunk", cwd=repo)
    with pytest.raises(runner.JobRefused):
        runner._job_fetch_archive("tools", T1, None)


@pytest.mark.parametrize("repo", ["a/b", "..", ".", "", "x" * 101, "-rf", "a b", "../x", "x.git",
                                  "https://evil/x", "a;b", "repo\n"])
def test_bad_repo_name_never_reaches_git(runner, monkeypatch, repo):
    def boom(*a, **k):
        raise AssertionError("git or a path was touched")

    monkeypatch.setattr(runner, "_repo_clone", boom)
    monkeypatch.setattr(runner.subprocess, "run", boom)
    monkeypatch.setattr(runner.subprocess, "Popen", boom)
    monkeypatch.setattr(runner, "_job_broker", boom)
    with pytest.raises(runner.JobRefused):
        runner._job_fetch_archive(repo, T1, None)


@pytest.mark.parametrize("turn_id", ["", "../x", "a/b", ".", "..", "x" * 65, "a b", "x" * 64, "turn-1",
                                     T1 + "\n", "AAAAAAAA-1111-4111-8111-111111111111", T1.replace("-", "")])
def test_bad_turn_id_never_becomes_a_path(runner, monkeypatch, turn_id):
    with pytest.raises(runner.JobRefused):
        runner._job_fetch_archive("tools", turn_id, None)


def test_export_refuses_a_symlink_pointing_out(runner, origins):
    repo = origins / "tools"
    (repo / "escape").symlink_to("/etc")
    _git("add", "-A", cwd=repo)
    _commit(repo, "link")
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_fetch_archive("tools", T1, None)
    assert "link" in str(exc.value)


def test_export_keeps_a_symlink_within_the_checkout(runner, origins):
    repo = origins / "tools"
    (repo / "latest.md").symlink_to("README.md")
    _git("add", "-A", cwd=repo)
    _commit(repo, "inner link")
    assert "latest.md" in _names(runner._job_fetch_archive("tools", T1, None))


def _tar(*members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE or kind == tarfile.LNKTYPE:
                info.linkname = target
            data = b"x" if kind == tarfile.REGTYPE else None
            if data:
                info.size = 1
            t.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


@pytest.mark.parametrize("members", [
    [("/etc/passwd", tarfile.REGTYPE, "")],
    [("../x", tarfile.REGTYPE, "")],
    [("a/../../x", tarfile.REGTYPE, "")],
    [("l", tarfile.SYMTYPE, "/etc")],
    [("l", tarfile.SYMTYPE, "../x")],
    [("d/l", tarfile.SYMTYPE, "../../x")],
    [("l", tarfile.SYMTYPE, "a/../../x")],
    [("l", tarfile.LNKTYPE, "/etc/passwd")],
    [(".git/config", tarfile.REGTYPE, "")],
    [("dev", tarfile.CHRTYPE, "")],
])
def test_archive_check_refuses(runner, members):
    with pytest.raises(runner.JobRefused):
        runner._check_job_archive(_tar(*members))


def test_archive_check_accepts_an_ordinary_tree(runner):
    runner._check_job_archive(_tar(("a", tarfile.DIRTYPE, ""), ("a/f", tarfile.REGTYPE, ""),
                                   ("a/l", tarfile.SYMTYPE, "f"), ("m", tarfile.SYMTYPE, "a/f")))


# ── the capability ───────────────────────────────────────────────────────────

STATUS = {"ok": True, "systemd": 255, "supported": True, "job_user": True, "locked": False, "running": "",
          "wedged": False}


@pytest.fixture
def can(runner, monkeypatch):
    monkeypatch.setattr(runner, "_job_user_exists", lambda: True)
    monkeypatch.setattr(runner, "JOBS_ENABLED", True)
    monkeypatch.setattr(runner, "_job_broker", lambda op, *a, **k: dict(STATUS))
    monkeypatch.setattr(runner, "_log", lambda m, *a, **k: None)
    runner.RUNNER_CAPS.pop("jobs", None)
    return runner


def test_caps_report_jobs_only_when_able(can, monkeypatch):
    can._apply_job_cap()
    assert can.RUNNER_CAPS["jobs"] is True

    monkeypatch.setattr(can, "_job_user_exists", lambda: False)
    can._apply_job_cap()
    assert can.RUNNER_CAPS.get("jobs") is not True


def _broker_says(can, monkeypatch, **fields):
    monkeypatch.setattr(can, "_job_broker", lambda op, *a, **k: {**STATUS, **fields})


@pytest.mark.parametrize("fields", [{"locked": True}, {"supported": False, "systemd": 249}, {"job_user": False},
                                    {"wedged": True}], ids=str)
def test_the_broker_saying_no_stops_the_report(can, monkeypatch, fields):
    """Any locked runner on the box, not just this one: so a job never fails on its first call."""
    _broker_says(can, monkeypatch, **fields)
    assert can._job_can_run() is False


def test_a_broker_that_doesnt_answer_stops_the_report(can, monkeypatch):
    def gone(op, *a, **k):
        raise can.JobRefused("not answering")
    monkeypatch.setattr(can, "_job_broker", gone)
    monkeypatch.setattr(can, "JOB_BROKER_START_WAIT", (0, 0))
    assert can._job_can_run() is False


def test_a_broker_still_starting_gets_a_moment(can, monkeypatch):
    """Started with --no-block at boot, so a runner can get there a moment before its socket."""
    answers = [can.JobRefused("not yet"), dict(STATUS)]

    def starting(op, *a, **k):
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(can, "_job_broker", starting)
    monkeypatch.setattr(can, "JOB_BROKER_START_WAIT", (0, 0))
    assert can._job_can_run() is True


def test_the_broker_is_asked_status_and_only_that(can, monkeypatch):
    asked = []
    monkeypatch.setattr(can, "_job_broker", lambda op, *a, **k: asked.append(op) or dict(STATUS))
    assert can._job_can_run() is True and asked == ["status"]


def test_sensitive_box_never_reports_jobs(can, monkeypatch):
    monkeypatch.setattr(can, "SENSITIVE_DATA_MODE", True)
    assert can._job_can_run() is False
    can._apply_job_cap()
    assert can.RUNNER_CAPS.get("jobs") is not True
    monkeypatch.setattr(can, "SENSITIVE_DATA_MODE", False)
    monkeypatch.setattr(can, "RUNNER_DATA_MODE", "sensitive_data")
    assert can._job_can_run() is False


def test_caps_off_while_the_switch_is_off(can, monkeypatch):
    monkeypatch.setattr(can, "JOBS_ENABLED", False)
    can._apply_job_cap()
    assert "jobs" not in can.RUNNER_CAPS


def test_switch_is_on_and_nothing_is_reported_until_checked(cloud_runner):
    assert cloud_runner.JOBS_ENABLED is True
    assert "jobs" not in cloud_runner.RUNNER_CAPS


def test_repo_clone_refuses_a_trailing_newline(cloud_runner):
    with pytest.raises(ValueError):
        cloud_runner._repo_clone("repo\n")


def test_a_box_without_pyyaml_never_reports_jobs(can, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)     # `import yaml` raises ImportError
    assert can._job_can_run() is False


def test_a_locked_runner_never_reports_jobs(can, monkeypatch):
    assert can._job_can_run() is True
    monkeypatch.setattr(can, "EGRESS_LOCKED", True)
    assert can._job_can_run() is False


# ── the capability, after the start ─────────────────────────────────────────

@pytest.fixture
def ticking(can, monkeypatch, tmp_path):
    """A started box signed in with its own device key, every PATCH recorded."""
    (tmp_path / "job.lock").write_text("")
    monkeypatch.setattr(can, "JOB_LOCK_FILE", tmp_path / "job.lock")
    can._apply_job_cap()
    assert can.RUNNER_CAPS["jobs"] is True
    patched = []
    monkeypatch.setattr(can, "_RUNNER_ID", "r1")
    monkeypatch.setitem(can._RUNNER_TOKEN, "raw", "box-token")
    monkeypatch.setitem(can._RUNNER_TOKEN, "exp", time.time() + 3600)
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "1")

    def api(method, path, body=None, **kw):
        patched.append((method, path, body))
        return can._test_patch_status, {}

    can._test_patch_status = 200
    monkeypatch.setattr(can, "_api", api)
    can._test_patched = patched
    return can


def _tick_due(mod):
    mod._JOB_CAP_TICK_AT = 0.0
    thread = mod._job_cap_tick()
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


def test_a_stopped_broker_stops_reporting_jobs_on_the_tick(ticking, monkeypatch):
    def gone(op, *a, **k):
        raise ticking.JobRefused("not answering")

    monkeypatch.setattr(ticking, "JOB_BROKER_START_WAIT", ())
    monkeypatch.setattr(ticking, "_job_broker", gone)
    _tick_due(ticking)
    assert ticking.RUNNER_CAPS["jobs"] is False
    assert [(m, p, b["capabilities"]["jobs"]) for m, p, b in ticking._test_patched] == [("PATCH", "/runners/r1", False)]


def test_setup_by_hand_starts_reporting_jobs_on_the_tick_with_no_restart(ticking, monkeypatch):
    ready = {"ok": False}
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: ready["ok"])
    _tick_due(ticking)
    assert ticking.RUNNER_CAPS["jobs"] is False and len(ticking._test_patched) == 1
    ready["ok"] = True                 # `--setup-runners`, by hand
    _tick_due(ticking)
    assert ticking.RUNNER_CAPS["jobs"] is True
    assert ticking._test_patched[-1][2]["capabilities"]["jobs"] is True


def test_the_tick_patches_only_when_it_changed(ticking):
    _tick_due(ticking)
    _tick_due(ticking)
    assert ticking._test_patched == []


def test_the_tick_keeps_to_its_period(ticking, monkeypatch):
    """Under five minutes end to end: the tick is checked every POLL_SECONDS."""
    assert ticking.JOB_CAP_TICK_SECONDS + ticking.POLL_SECONDS <= 300
    monkeypatch.setattr(ticking, "_job_can_run", lambda: pytest.fail("not due yet"))
    ticking._JOB_CAP_TICK_AT = time.monotonic()
    assert ticking._job_cap_tick() is None


def test_a_refused_patch_is_sent_again_on_the_next_tick(ticking, monkeypatch):
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: False)
    ticking._test_patch_status = 503
    _tick_due(ticking)
    ticking._test_patch_status = 200
    _tick_due(ticking)
    assert len(ticking._test_patched) == 2
    _tick_due(ticking)
    assert len(ticking._test_patched) == 2


def test_a_drop_whose_patch_failed_is_sent_again_on_the_tick(ticking, monkeypatch):
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: False)
    ticking._test_patch_status = 503
    ticking._job_drop_capability("a process survived")
    ticking._test_patch_status = 200
    _tick_due(ticking)
    assert len(ticking._test_patched) == 2 and ticking._test_patched[-1][2]["capabilities"]["jobs"] is False


def test_the_tick_sends_nothing_without_the_boxs_own_token(ticking, monkeypatch):
    """canopy-web believes `jobs` only from the box's device-key token: a person's PAT would be
    a PATCH that changes nothing and looks as if it had."""
    monkeypatch.setattr(ticking, "_bearer", lambda *a, **k: "a-persons-pat")
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: False)
    _tick_due(ticking)
    assert ticking._test_patched == []
    assert ticking.RUNNER_CAPS["jobs"] is False


def _said(mod, monkeypatch):
    lines = []
    mod._JOB_CAP_SAID.clear()
    monkeypatch.setattr(mod, "_log", lambda m, *a, **k: lines.append(m))
    monkeypatch.setattr(mod, "JOB_BROKER_START_WAIT", ())
    return lines


def test_no_broker_is_logged_once_per_change_not_each_tick(ticking, monkeypatch):
    lines = _said(ticking, monkeypatch)
    answer = {}

    def broker(op, *a, **k):
        if answer.get("up"):
            return dict(STATUS)
        raise ticking.JobRefused("not answering")

    monkeypatch.setattr(ticking, "_job_broker", broker)
    for _ in range(3):
        _tick_due(ticking)
    refused = [m for m in lines if "can't run them" in m]
    assert len(refused) == 1 and "not answering" in refused[0]
    answer["up"] = True
    _tick_due(ticking)
    answer["up"] = False                   # gone again: that's a change, so it is said again
    _tick_due(ticking)
    _tick_due(ticking)
    assert len([m for m in lines if "can't run them" in m]) == 2


def test_a_changed_reason_is_logged(ticking, monkeypatch):
    lines = _said(ticking, monkeypatch)
    monkeypatch.setattr(ticking, "_job_broker", lambda op, *a, **k: {**STATUS, "locked": True})
    _tick_due(ticking)
    _tick_due(ticking)
    monkeypatch.setattr(ticking, "_job_broker", lambda op, *a, **k: {**STATUS, "supported": False})
    _tick_due(ticking)
    assert len([m for m in lines if "can't run them" in m]) == 2


def test_device_key_off_is_logged_once_per_change_not_each_tick(ticking, monkeypatch):
    lines = _said(ticking, monkeypatch)
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: False)
    monkeypatch.setattr(ticking, "TOKEN", "a-persons-pat")
    for _ in range(3):
        _tick_due(ticking)
    assert ticking._test_patched == []
    assert len([m for m in lines if "own token" in m]) == 1
    # Back on its own token: the owed PATCH goes, and a later loss is news again.
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "1")
    _tick_due(ticking)
    assert len(ticking._test_patched) == 1
    monkeypatch.setattr(ticking, "_job_user_exists", lambda: True)
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    _tick_due(ticking)
    assert len([m for m in lines if "own token" in m]) == 2


def test_the_tick_is_off_while_the_switch_is_off(ticking, monkeypatch):
    monkeypatch.setattr(ticking, "JOBS_ENABLED", False)
    ticking._JOB_CAP_TICK_AT = 0.0
    assert ticking._job_cap_tick() is None


def test_both_main_loops_run_the_tick():
    source = (EC2 / "cloud_runner.py").read_text()
    main = source[source.index("def run_over_rest("):]
    assert main.count("_credential_tick()\n") == main.count("_job_cap_tick()\n") >= 2


def test_a_box_not_provisioned_never_reports_jobs(can, monkeypatch, tmp_path):
    (tmp_path / "job.lock").unlink()
    assert can._job_can_run() is False


# ── running a job (Task 3) ───────────────────────────────────────────────────
# A real `canopy-job` isn't here: the harness's broker stubs what it does as root, so what
# is proved is the runner's and the broker's half. `job_self_check.sh` proves systemd's on a box.

SIX = {"PATH", "HOME", "LANG", "CANOPY_AGENT_SLUG", "CANOPY_TURN_ID", "CANOPY_JOB_TOKENS"}


@pytest.fixture
def jobs(runner, monkeypatch, tmp_path, origins):
    monkeypatch.setattr(runner, "JOB_POLL_SECONDS", 0.02)
    monkeypatch.setattr(runner, "JOB_BATCH_SECONDS", 0.05)
    monkeypatch.setattr(runner, "JOB_TOKEN_TICK_SECONDS", 0.05)
    monkeypatch.setattr(runner, "_agent_env", lambda slug: None)
    monkeypatch.setattr(runner, "_CONN_HOLDS", {})
    monkeypatch.setattr(runner, "_JOB_LOCK", threading.Lock())
    monkeypatch.setattr(runner.pathlib.Path, "home", lambda: tmp_path / "runner-home")
    monkeypatch.setattr(runner, "_log", lambda m, *a, **k: None)
    monkeypatch.setattr(runner, "_job_user_exists", lambda: True)
    broker = load_broker()
    # Beside the system's, so that `python3` is found under `env -i` here as it is on a box.
    monkeypatch.setattr(broker, "JOB_PATH", f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin")
    harness = Harness(broker, runner.JOBS_ROOT, runner._marker)
    harness.holders = []
    monkeypatch.setattr(runner, "JOB_BROKER_SOCKET", harness.path)
    runner._origin = origins / "tools"
    runner._box = harness
    yield runner
    for holder in harness.holders:
        holder.close()
    harness.close()


def _define(mod, run, *, files=None, timeout=None):
    repo = mod._origin
    for name, text in (files or {}).items():
        (repo / name).write_text(text)
    (repo / ".canopy" / "jobs.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "jobs": {"sync": {"run": run, **({"timeout_minutes": timeout} if timeout else {})}}}))
    _git("add", "-A", cwd=repo)
    _commit(repo, "job")


def _go(mod, turn_id=T1):
    out = []
    ok, text = mod._run_job("r1", {"id": turn_id, "agent_slug": "eva", "job": "sync", "job_repo": "tools"},
                            lambda events: out.extend(events))
    return ok, text, out


def _text(events):
    return "".join(e["payload"]["text"] for e in events)


def _alive(mod):
    got = subprocess.run(["pgrep", "-af", mod._marker], capture_output=True, text=True)
    mod._diag_seen = getattr(mod, "_diag_seen", [])
    mod._diag_seen.append((round(time.monotonic(), 3), got.returncode, got.stdout.strip()[:240]))
    return got.returncode == 0


def _stage(mod, tmp_path, token, *, provider="salesforce"):
    path = tmp_path / "runner-home" / ".canopy" / "connections" / "eva" / f"{provider}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token, "expires_at": 9e12, "instance_url": "https://x"}))
    mod._CONN_HOLDS[T1] = ("eva", "r1", frozenset({provider}))
    return path


def test_job_env_is_exactly_the_six(jobs):
    _define(jobs, ["env"])
    ok, _, out = _go(jobs)
    assert ok
    seen = dict(line.split("=", 1) for line in _text(out).splitlines())
    assert set(seen) == SIX
    assert seen["CANOPY_AGENT_SLUG"] == "eva" and seen["CANOPY_TURN_ID"] == T1
    assert seen["CANOPY_JOB_TOKENS"] == str(jobs.JOBS_ROOT / T1 / "tokens")
    assert seen["HOME"] == str(jobs.JOBS_ROOT / T1 / "home")


def test_no_runner_token_or_model_key_in_env(jobs, monkeypatch):
    for name in ("CANOPY_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CANOPY_WEB_PAT", "GH_TOKEN"):
        monkeypatch.setenv(name, f"leak-{name}")
    _define(jobs, ["env"])
    _, _, out = _go(jobs)
    assert "leak-" not in _text(out)
    # and the command names nothing else: `env -i` first, then the six, then the job
    argv = jobs._box.spawned[0]
    tail = argv[argv.index("--") + 1:]
    assert tail[:2] == ["/usr/bin/env", "-i"]
    assert {a.split("=")[0] for a in tail[2:8]} == SIX and tail[8:] == ["env"]


def test_argv_passed_with_no_shell(jobs, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("still here")
    evil = f"; rm -rf {victim}"
    _define(jobs, ["python3", "argv.py", evil, "$(touch pwned)"],
            files={"argv.py": "import sys\nprint(repr(sys.argv[1:]))\n"})
    ok, _, out = _go(jobs)
    assert ok and victim.exists()
    assert repr([evil, "$(touch pwned)"]) in _text(out)
    assert evil in jobs._box.spawned[0] and "sh" not in jobs._box.spawned[0]


def test_job_runs_in_its_export(jobs):
    _define(jobs, ["pwd"])
    ok, _, out = _go(jobs)
    assert ok
    assert pathlib.Path(_text(out).strip()).resolve() == (jobs.JOBS_ROOT / T1 / "repo").resolve()


def test_exit_zero_done(jobs):
    _define(jobs, ["python3", "x.py"], files={"x.py": "print('all good')\n"})
    ok, text, _ = _go(jobs)
    assert ok is True and "exit code 0" in text and "all good" in text


def test_non_zero_failed_with_last_lines(jobs):
    body = "import sys\nfor i in range(100): print('line', i)\nsys.exit(3)\n"
    _define(jobs, ["python3", "x.py"], files={"x.py": body})
    ok, text, out = _go(jobs)
    assert ok is False and "exit code 3" in text
    assert "line 99" in text and "line 10\n" not in text       # the last lines only
    assert "line 0\n" in _text(out)                             # the stream has them all


IGNORES_TERM = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('running', flush=True)
time.sleep(300)
"""


def test_timeout_kills_and_fails(jobs, monkeypatch):
    """The unit's RuntimeMaxSec is the limit (pinned in test_job_broker); it reads as a timeout."""
    monkeypatch.setattr(jobs, "JOB_SECONDS_PER_MINUTE", 1)
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM}, timeout=1)
    ok, text, out = _go(jobs)
    assert ok is False and "1-minute" in text
    assert "running" in _text(out)
    assert not _alive(jobs)
    assert "--property=RuntimeMaxSec=1" in jobs._box.spawned[0]


BACKGROUND = """
import subprocess, sys
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1]],
                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
print('left one behind')
"""


def test_background_process_doesnt_outlive_job(jobs):
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": BACKGROUND})
    ok, _, _ = _go(jobs)
    assert ok is True and not _alive(jobs)


def test_background_process_holding_the_pipe_doesnt_hang_the_end(jobs):
    body = BACKGROUND.replace("stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, ", "")
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": body})
    started = time.monotonic()
    ok, _, _ = _go(jobs)
    assert ok is True and not _alive(jobs) and time.monotonic() - started < 30


def test_output_capped_at_1mb_with_a_line(jobs):
    body = "for i in range(40000): print('x' * 99)\nprint('the very end')\n"
    _define(jobs, ["python3", "x.py"], files={"x.py": body})
    ok, text, out = _go(jobs)
    shown = _text(out)
    assert ok is True
    assert len(shown.encode()) <= jobs.JOB_OUTPUT_CAP_BYTES + 200
    assert shown.count("output was cut") == 1
    assert "the very end" in text          # the note still has the real last lines


def test_token_value_scrubbed_from_output(jobs, tmp_path):
    token = "tok_" + "s3cret" * 8
    _stage(jobs, tmp_path, token)
    body = ("import json, os, sys\n"
            "t = json.load(open(os.environ['CANOPY_JOB_TOKENS'] + '/salesforce.json'))['access_token']\n"
            "print('here', t, flush=True)\n"
            "sys.stdout.write(t[:9]); sys.stdout.flush()\n"
            "import time; time.sleep(0.3)\n"
            "print(t[9:], flush=True)\n"
            "sys.stderr.write(t + '\\n'); sys.exit(2)\n")
    _define(jobs, ["python3", "x.py"], files={"x.py": body})
    ok, text, out = _go(jobs)
    assert ok is False
    assert token not in _text(out) and token not in text
    assert token[:9] not in _text(out)            # the half split across two reads
    assert _text(out).count("[token]") == 3 and "[token]" in text


def test_scrubber_holds_a_tail_across_chunks(jobs):
    o = jobs._JobOutput(lambda ev: None)
    o.add_tokens([b"ABCDEFGHIJ"])
    o.feed(b"before ABCDE")
    assert "ABCDE" not in o.take()
    o.feed(b"FGHIJ after")
    o.close()
    assert o.take() == "[token] after" or "ABCDEFGHIJ" not in o.tail


def test_scrubber_keeps_a_character_split_across_chunks(jobs):
    o = jobs._JobOutput(lambda ev: None)
    data = "café".encode()
    o.feed(data[:-1])
    o.feed(data[-1:])
    o.close()
    assert o.take() == "café"


def _exported(mod):
    data = mod._job_fetch_archive("tools", T1, None)
    holder = mod._job_broker_hold(data, T1)
    mod._box.holders.append(holder)


def test_a_refreshed_token_is_scrubbed_too(jobs, tmp_path):
    copied = {}
    o = jobs._JobOutput(lambda ev: None)
    path = _stage(jobs, tmp_path, "first-token-value")
    _exported(jobs)
    jobs._job_copy_tokens(T1, "eva", copied, o)
    path.write_text(json.dumps({"access_token": "second-token-value", "expires_at": 9e12}))
    jobs._job_copy_tokens(T1, "eva", copied, o)
    o.feed(b"first-token-value second-token-value\n")
    o.close()
    assert o.take() == "[token] [token]\n"


def test_token_copy_refreshed_on_tick(jobs, tmp_path):
    path = _stage(jobs, tmp_path, "old-token-value")
    body = ("import json, os, stat, time\n"
            "p = os.environ['CANOPY_JOB_TOKENS'] + '/salesforce.json'\n"
            "print(oct(stat.S_IMODE(os.stat(p).st_mode)), flush=True)\n"
            "for _ in range(500):\n"
            "    if json.load(open(p))['access_token'] != 'old-token-value':\n"
            "        print('saw the new one'); raise SystemExit(0)\n"
            "    time.sleep(0.02)\n"
            "raise SystemExit(9)\n")
    _define(jobs, ["python3", "x.py"], files={"x.py": body})

    def refresh_once_the_job_has_its_copy():
        copy = jobs.JOBS_ROOT / T1 / "tokens" / "salesforce.json"
        for _ in range(1000):
            if copy.exists():
                break
            time.sleep(0.01)
        path.write_text(json.dumps({"access_token": "new-token-value", "expires_at": 9e12}))

    threading.Thread(target=refresh_once_the_job_has_its_copy, daemon=True).start()
    ok, _, out = _go(jobs)
    assert ok is True and "saw the new one" in _text(out) and "0o600" in _text(out)
    # always through a temp name and `mv`, written by the job user with umask 077
    copies = [c for c in jobs._box.as_job_calls if "os.rename" in " ".join(c)]
    assert len(copies) == 2


def test_a_withdrawn_token_is_removed_from_the_job(jobs, tmp_path):
    path = _stage(jobs, tmp_path, "soon-revoked-value")
    copied = {}
    o = jobs._JobOutput(lambda ev: None)
    tokens = jobs.JOBS_ROOT / T1 / "tokens"
    _exported(jobs)
    jobs._job_copy_tokens(T1, "eva", copied, o)
    assert (tokens / "salesforce.json").exists()
    path.unlink()
    jobs._job_copy_tokens(T1, "eva", copied, o)
    assert not (tokens / "salesforce.json").exists() and copied == {}


def _left_behind(mod):
    return list(mod.JOBS_ROOT.iterdir())


@pytest.mark.parametrize("how", ["done", "failed", "refused", "raised", "refused_export"])
def test_cleanup_on_every_path(jobs, monkeypatch, tmp_path, how):
    _stage(jobs, tmp_path, "a-token-value")
    _define(jobs, ["python3", "x.py", jobs._marker],
            files={"x.py": BACKGROUND + ("raise SystemExit(4)\n" if how == "failed" else "")
                   + ("import time; time.sleep(300)\n" if how == "raised" else "")})
    if how == "refused":
        (jobs._origin / ".canopy" / "jobs.yaml").write_text("version: 1\njobs: {other: {run: [make]}}\n")
        _commit(jobs._origin, "no such job")
    if how == "refused_export":
        monkeypatch.setattr(jobs, "REPO_URL_TEMPLATE", str(tmp_path / "nowhere" / "{repo}"))
        monkeypatch.setattr(jobs, "WORK_DIR", str(tmp_path / "other-work"))
    if how == "raised":
        real = jobs._job_low_disk
        count = {"n": 0}

        def boom():
            count["n"] += 1
            if count["n"] > 5:
                raise RuntimeError("boom in the middle")
            return real()
        monkeypatch.setattr(jobs, "_job_low_disk", boom)
        with pytest.raises(RuntimeError):
            _go(jobs)
    else:
        ok, text, _ = _go(jobs)
        assert ok is (how == "done") and text.endswith(".") or "exit code" in text
    assert not _alive(jobs)
    assert _left_behind(jobs) == []
    assert jobs._JOB_LOCK.locked() is False


def test_the_lock_is_free_after_a_job_so_the_next_runs(jobs):
    _define(jobs, ["env"])
    assert _go(jobs)[0] and _go(jobs, T2)[0]


def test_a_second_job_finishes_failed_and_doesnt_touch_the_first(jobs):
    _define(jobs, ["env"])
    assert jobs._JOB_LOCK.acquire(blocking=False)
    try:
        ok, text, _ = _go(jobs, T2)
    finally:
        jobs._JOB_LOCK.release()
    assert ok is False and text == "Another job is running on this machine."
    assert jobs._box.requests == []                # no stop, no cleanup, no export: nothing


def test_another_runner_on_the_box_holding_a_job_stops_this_one(jobs, tmp_path):
    """Several runners share one box and one canopy-job, each its own process: the guard
    has to be between processes, or one runner's end would kill another's job."""
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys,time\nf=open(sys.argv[1])\nfcntl.flock(f,fcntl.LOCK_EX)\n"
                               "print('held',flush=True)\ntime.sleep(60)\n", str(jobs.JOB_LOCK_FILE)],
        stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"held"
        ok, text, _ = _go(jobs, T2)
        assert ok is False and text == "Another job is running on this machine."
        jobs._job_restart_sweep()                  # nor does a runner restarting beside it
        assert jobs._box.requests == []
    finally:
        holder.kill()
        holder.wait()


def test_a_job_does_not_inherit_the_lock(jobs):
    """A background process left holding the lock would refuse every later job."""
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": BACKGROUND})
    assert _go(jobs)[0] and _go(jobs, T2)[0]


def test_stop_request_kills_the_job(jobs, monkeypatch):
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM})
    threading.Timer(0.5, lambda: jobs._STOP_REQUESTED.setdefault(T1, time.time())).start()
    try:
        ok, text, _ = _go(jobs)
    finally:
        jobs._STOP_REQUESTED.pop(T1, None)
    assert ok is False and "stopped" in text and not _alive(jobs)


def test_runner_stopping_kills_the_job_and_says_so(jobs, monkeypatch):
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM})
    threading.Timer(0.5, lambda: setattr(jobs, "_stop", True)).start()
    try:
        ok, text, _ = _go(jobs)
    finally:
        jobs._stop = False
    assert ok is False and text.startswith("This machine restarted during the job.")
    assert not _alive(jobs)


def test_a_filling_disk_stops_the_job(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_job_low_disk", lambda: True)
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM})
    ok, text, _ = _go(jobs)
    assert ok is False and "disk" in text and not _alive(jobs)


def test_a_failing_emit_never_costs_the_job(jobs):
    _define(jobs, ["env"])

    def down(events):
        raise OSError("canopy-web is away")
    ok, _ = jobs._run_job("r1", {"id": T1, "agent_slug": "eva", "job": "sync", "job_repo": "tools"}, down)
    assert ok is True


def test_a_run_naming_something_absent_is_a_failed_job(jobs):
    _define(jobs, ["./no-such-program"])
    ok, text, _ = _go(jobs)
    assert ok is False and "exit code" in text


# ── restart ──────────────────────────────────────────────────────────────────

def test_restart_sweep_removes_whatever_is_left(jobs):
    (jobs.JOBS_ROOT / T1 / "tokens").mkdir(parents=True)
    jobs._job_restart_sweep()
    assert jobs._box.requests[0][0] == "cleanup" and _left_behind(jobs) == []


@pytest.mark.parametrize("rep", range(400))
def test_a_new_daemon_ends_the_old_ones_job(jobs, rep):
    """The old daemon's death closed its connection, which stops the unit."""
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM})
    _exported(jobs)
    c = jobs._job_broker_connect("start", turn=T1, slug="eva", run=["python3", "x.py", jobs._marker],
                                 timeout_seconds=60)
    assert b"running" in c.recv(4096)
    jobs._diag_seen = []
    closed_at = time.monotonic()
    c.close()
    for _ in range(400):
        if not _alive(jobs):
            break
        time.sleep(0.05)
    if _alive(jobs):
        with open("/tmp/diag.txt", "a") as f:
            print("DIAG FAIL rep", rep, "after", round(time.monotonic() - closed_at, 3),
                  "polls", len(jobs._diag_seen), "seen", [(rc, line) for _, rc, line in jobs._diag_seen[-3:]], file=f)
        pytest.fail("DIAG")
    jobs._box.holders.pop().close()      # and the daemon's death closed its hold on the slot
    for _ in range(400):
        if not _left_behind(jobs):
            break
        time.sleep(0.05)
    jobs._job_restart_sweep()
    assert _left_behind(jobs) == []


def test_the_slot_is_held_for_the_whole_job(jobs, monkeypatch):
    """Every op of a job's life is inside one export-to-cleanup slot, and the slot's connection
    stays open throughout: another runner's export is refused at every step."""
    _define(jobs, ["env"])
    seen = []
    real = jobs._job_supervise

    def during(*a):
        seen.append(jobs._box.server._job["turn"])
        got = real(*a)
        seen.append(jobs._box.server._job["turn"])     # the job has exited; still held
        return got
    monkeypatch.setattr(jobs, "_job_supervise", during)
    assert _go(jobs)[0] is True
    assert seen == [T1, T1] and jobs._box.server._job is None


def test_a_process_that_wont_die_stops_this_box_reporting_jobs(jobs, monkeypatch):
    """It is the only moment a runner learns: the capability is otherwise read at its start."""
    _define(jobs, ["env"])
    posted = []
    monkeypatch.setattr(jobs, "_api", lambda m, p, b=None, **k: posted.append((m, p, b)) or (200, {}))
    monkeypatch.setattr(jobs, "_RUNNER_ID", "r1")
    monkeypatch.setitem(jobs.RUNNER_CAPS, "jobs", True)
    jobs._box.survivors = True
    ok, text, _ = _go(jobs)
    assert ok is False and "couldn't be stopped" in text
    assert jobs.RUNNER_CAPS["jobs"] is False
    assert [b["capabilities"]["jobs"] for m, p, b in posted if m == "PATCH"] == [False]


def test_a_process_surviving_the_end_of_a_job_says_so_too(jobs, monkeypatch):
    """Clean at the start, a survivor at the end: the job's own verdict stands, the box stops."""
    _define(jobs, ["env"])
    posted = []
    monkeypatch.setattr(jobs, "_api", lambda m, p, b=None, **k: posted.append((m, p, b)) or (200, {}))
    monkeypatch.setattr(jobs, "_RUNNER_ID", "r1")
    monkeypatch.setitem(jobs.RUNNER_CAPS, "jobs", True)
    real = jobs._job_supervise

    def then_a_survivor(*a):
        got = real(*a)
        jobs._box.survivors = True
        return got
    monkeypatch.setattr(jobs, "_job_supervise", then_a_survivor)
    ok, _, _ = _go(jobs)
    assert ok is True and jobs.RUNNER_CAPS["jobs"] is False
    assert jobs._box.server._job is not None


def test_a_broker_that_goes_away_mid_job_is_a_plain_failure(jobs, monkeypatch):
    _define(jobs, ["python3", "x.py", jobs._marker], files={"x.py": IGNORES_TERM})

    def cut(conn, output, result, done):
        conn.close()
        done.set()
    monkeypatch.setattr(jobs, "_job_read_frames", cut)
    ok, text, _ = _go(jobs)
    assert ok is False and "stopped answering" in text


def test_restart_sweep_does_nothing_on_a_box_without_the_user(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_job_user_exists", lambda: False)
    jobs._job_restart_sweep()
    assert jobs._box.requests == []


def _state(mod, tmp_path, monkeypatch, **fields):
    monkeypatch.setattr(mod, "TURN_STATE_ROOT", tmp_path / "state")
    mod._update_turn_state(T1, turn={"id": T1, "agent_slug": "eva", "job": "sync", "job_repo": "tools"},
                           runner_id="r1", phase="setup", adoptable=False, **fields)


@pytest.mark.parametrize("phase", ["setup", "running"])
def test_a_held_job_finishes_failed_on_restart_never_retried(jobs, monkeypatch, tmp_path, phase):
    _state(jobs, tmp_path, monkeypatch)
    jobs._update_turn_state(T1, phase=phase, attempt="a1", slug="eva")
    posted = []
    monkeypatch.setattr(jobs, "_api", lambda m, p, b=None, **k: posted.append((m, p, b)) or (200, {}))
    monkeypatch.setattr(jobs, "execute_prompt",
                        lambda *a, **k: pytest.fail("a job went to a model"))
    monkeypatch.setattr(jobs, "_run_job", lambda *a, **k: pytest.fail("a job ran again"))
    summary = jobs.adopt_turns("r1")
    finish = [b for m, p, b in posted if p == f"/turns/{T1}/finish"]
    assert finish == [{"status": "failed", "result_note": "This machine restarted during the job."}]
    assert summary["adopted"] == [] and not (tmp_path / "state" / T1).exists()


def test_a_job_turn_is_never_adoptable_and_pins_the_updater(jobs, monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "ADOPT_TURNS", True)
    monkeypatch.setenv("CANOPY_RUNNER_SUPERVISED", "1")
    assert jobs.adoptable_turn({"id": T1, "job": ""}) is True
    assert jobs.adoptable_turn({"id": T1, "job": "sync"}) is False
    monkeypatch.setattr(jobs, "TURN_STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(jobs, "_IN_FLIGHT", {})
    monkeypatch.setattr(jobs, "_run_turn", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_lifecycle", lambda *a, **k: None)
    assert jobs._start_turn("r1", {"id": T1, "agent_slug": "eva", "job": "sync", "job_repo": "tools"})
    state = jobs._read_json(tmp_path / "state" / T1 / "turn.json")
    assert state["adoptable"] is False and state["turn"]["job"] == "sync"
    jobs._IN_FLIGHT.update({T1: None})
    assert jobs._adoptable_in_flight() == (1, 1)


# ── the seam in _run_turn ────────────────────────────────────────────────────

from test_confined_turns import cr as turn_cr  # noqa: E402,F401


@pytest.fixture
def seam(turn_cr, monkeypatch, tmp_path):  # noqa: F811
    mod, calls = turn_cr
    monkeypatch.setattr(mod, "_CONN_HOLDS", {})
    monkeypatch.setattr(mod, "execute_prompt", lambda *a, **k: pytest.fail("a job went to a model"))
    monkeypatch.setattr(mod, "_turn_cwd", lambda *a, **k: pytest.fail("a job needs no agent checkout"))
    order = calls["order"] = []
    real_stage, real_unstage = mod._stage_connections, mod._unstage_connections
    monkeypatch.setattr(mod, "_stage_connections",
                        lambda *a: order.append("stage") or real_stage(*a))
    monkeypatch.setattr(mod, "_unstage_connections",
                        lambda t: order.append("unstage") or real_unstage(t))
    return mod, calls


JOB_TURN = {"id": T1, "agent_slug": "eva", "job": "sync", "job_repo": "tools", "prompt": "",
            "caller_context": {"profile": "full", "capability": None}}


def _finish(calls):
    return [b for m, p, b in calls["api"] if p == f"/turns/{T1}/finish"]


def test_transcript_ship_skipped(seam, monkeypatch):
    mod, calls = seam
    order = calls["order"]
    monkeypatch.setattr(mod, "_run_job",
                        lambda rid, turn, emit: order.append("job") or (True, "The job finished (exit code 0)."))
    mod._run_turn("r-1", dict(JOB_TURN))
    assert order[:3] == ["stage", "job", "unstage"]
    assert calls["shipped"] == [] and calls["recorded"] == []
    body = _finish(calls)
    assert body[0]["status"] == "done" and "session_key" not in body[0]


def test_a_raise_in_the_job_is_a_failed_turn_and_unstages(seam, monkeypatch):
    mod, calls = seam

    def boom(*a):
        raise RuntimeError("no")
    monkeypatch.setattr(mod, "_run_job", boom)
    mod._run_turn("r-1", dict(JOB_TURN))
    assert _finish(calls)[0]["status"] == "failed" and "unstage" in calls["order"]


def test_a_stopped_job_finishes_cancelled(seam, monkeypatch):
    mod, calls = seam
    monkeypatch.setattr(mod, "_IN_FLIGHT", {T1: None})
    mod._STOP_REQUESTED[T1] = time.time()
    monkeypatch.setattr(mod, "_run_job", lambda *a: (False, "The job was stopped."))
    mod._run_turn("r-1", dict(JOB_TURN))
    assert _finish(calls)[0]["status"] == "cancelled"


def test_a_job_is_not_left_for_requeue_when_the_box_shuts_down(seam, monkeypatch):
    """`_killed_by_shutdown` leaves an ordinary turn running to run again elsewhere."""
    mod, calls = seam
    monkeypatch.setattr(mod, "_stop", True)
    monkeypatch.setattr(mod, "_system_shutting_down", lambda: True)
    monkeypatch.setattr(mod, "_run_job", lambda *a: (False, "This machine restarted during the job."))
    mod._run_turn("r-1", dict(JOB_TURN))
    assert _finish(calls)[0]["status"] == "failed"


def test_a_job_needs_no_cli_on_the_box(seam, monkeypatch):
    mod, calls = seam
    monkeypatch.setattr(mod, "_run_job", lambda *a: (True, "ok"))
    monkeypatch.setattr(mod, "_cli_refusal", lambda turn: pytest.fail("a job has no CLI to refuse"))
    mod._run_turn("r-1", {**JOB_TURN, "cli": ""})
    assert _finish(calls)[0]["status"] == "done"


def test_a_confined_turn_carrying_a_job_never_runs_it(seam, monkeypatch):
    mod, calls = seam
    monkeypatch.setattr(mod, "_run_job", lambda *a: pytest.fail("a confined turn ran a job"))
    mod._run_turn("r-1", {**JOB_TURN, "caller_context": {"profile": "confined", "capability": {"name": "ask"}}})
    assert _finish(calls)[0]["status"] == "failed"


def test_events_are_plain_lines_of_an_allowed_kind(jobs):
    _define(jobs, ["env"])
    _, _, out = _go(jobs)
    assert out and {e["kind"] for e in out} == {"status"}
    assert all(e["payload"]["source"] == "job" for e in out)


# ── the box: user, sudoers, network ──────────────────────────────────────────

class FakeBox:
    def __init__(self, tmp_path, *, iptables=0, exists=False):
        self.calls, self.iptables, self.exists = [], iptables, exists
        self.rules = {"iptables": [], "ip6tables": []}
        self.owners = {}
        self.root = tmp_path / "var-lib-canopy-jobs"
        self.lock = tmp_path / "run" / "job.lock"

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == "id":
            return subprocess.CompletedProcess(argv, 0 if self.exists else 1, "", "")
        if tool == "useradd":
            self.exists = True
        if tool in self.rules:
            if self.iptables:
                return subprocess.CompletedProcess(argv, self.iptables, "", "no")
            rest = argv[1:]
            if rest[0] == "-N":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if rest[0] == "-C":
                return subprocess.CompletedProcess(argv, 0 if rest[1:] in self.rules[tool] else 1, "", "")
            if rest[0] == "-A":
                self.rules[tool].append(rest[1:])
            if rest[0] == "-I":
                self.rules[tool].insert(0, [rest[1], *rest[3:]])
            if rest[0] == "-D":
                self.rules[tool].remove(rest[1:])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def chown(self, path, uid, gid):
        self.owners[str(path).removesuffix(".tmp")] = uid


def _setup_jobs(mod, fake, box=None, *, locked=False):
    return mod.setup_jobs(box or {}, locked=locked, run=fake.run, chown=fake.chown,
                          uid_of=lambda user: SimpleNamespace(pw_uid=999, pw_gid=999),
                          root=fake.root, lock=fake.lock)


@pytest.fixture
def fake(tmp_path, cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda tool: f"/usr/sbin/{tool}")
    monkeypatch.setattr(cloud_runner, "_log", lambda m, *a, **k: None)
    return FakeBox(tmp_path)


def test_setup_makes_a_system_user_the_root_and_the_lock(cloud_runner, fake):
    assert _setup_jobs(cloud_runner, fake) is True
    add = next(c for c in fake.calls if c[0] == "useradd")
    assert "--system" in add and add[-1] == "canopy-job" and "/usr/sbin/nologin" in add
    assert "--create-home" not in add
    # no home: the jobs root as its home is how a planted user-site once ran in a helper
    assert add[add.index("--home-dir") + 1] == "/nonexistent"
    # root's: only a unit's own bound folder is ever canopy-job's to write
    assert stat.S_IMODE(fake.root.stat().st_mode) == 0o700 and fake.owners[str(fake.root)] == 0
    # readable by every runner, so each can take the flock; root's, so none can replace it
    assert stat.S_IMODE(fake.lock.stat().st_mode) == 0o644 and fake.owners[str(fake.lock)] == 0


def test_setup_keeps_an_existing_lock_file_in_place(cloud_runner, fake):
    """A held flock is on the inode: replacing the file would let two jobs in."""
    _setup_jobs(cloud_runner, fake)
    inode = fake.lock.stat().st_ino
    _setup_jobs(cloud_runner, fake)
    assert fake.lock.stat().st_ino == inode
    assert sum(1 for c in fake.calls if c[0] == "useradd") == 1


def test_setup_moves_an_existing_users_home_off_the_jobs_root(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda tool: f"/usr/sbin/{tool}")
    monkeypatch.setattr(cloud_runner, "_log", lambda m, *a, **k: None)
    box = FakeBox(tmp_path, exists=True)
    assert cloud_runner.setup_jobs({}, locked=False, run=box.run, chown=box.chown, root=box.root, lock=box.lock,
                                   uid_of=lambda u: SimpleNamespace(pw_uid=999, pw_gid=999, pw_dir=str(box.root)))
    assert ["usermod", "--home", "/nonexistent", "canopy-job"] in box.calls


def test_iptables_failing_says_no(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_log", lambda m, *a, **k: None)
    assert _setup_jobs(cloud_runner, FakeBox(tmp_path, iptables=1)) is False


def test_sensitive_box_gets_no_user(cloud_runner, fake):
    assert _setup_jobs(cloud_runner, fake, {"RUNNER_DATA_MODE": "sensitive_data"}) is False
    assert not any(c[0] == "useradd" for c in fake.calls)


def _flat(rule):
    return " ".join(rule)


def test_rules_refuse_metadata_and_loopback_but_dns(cloud_runner, fake):
    cloud_runner.job_rules(999, locked=False, run=fake.run)
    v4 = [_flat(r) for r in fake.rules["iptables"]]
    own = "-m owner --uid-owner 999"
    assert ["OUTPUT -j CANOPY-JOB"] == [x for x in v4 if x.startswith("OUTPUT")]
    chain = [x.removeprefix("CANOPY-JOB ") for x in v4 if not x.startswith("OUTPUT")]
    assert f"{own} -d 169.254.169.254 -j REJECT" in chain
    dns = [f"{own} -o lo -d 127.0.0.53 -p udp --dport 53 -j RETURN",
           f"{own} -o lo -d 127.0.0.53 -p tcp --dport 53 -j RETURN"]
    closed = f"{own} -o lo -j REJECT"
    assert chain.index(dns[0]) < chain.index(closed) and chain.index(dns[1]) < chain.index(closed)
    v6 = [_flat(r) for r in fake.rules["ip6tables"]]
    assert f"CANOPY-JOB {own} -d fd00:ec2::254 -j REJECT" in v6 and f"CANOPY-JOB {closed}" in v6
    assert not any("--dport 53" in x for x in v6)


def test_rules_are_idempotent_and_never_flushed(cloud_runner, fake):
    """Flushing would open a gap every minute with a job running."""
    cloud_runner.job_rules(999, locked=False, run=fake.run)
    before = {k: list(v) for k, v in fake.rules.items()}
    cloud_runner.job_rules(999, locked=False, run=fake.run)
    assert fake.rules == before
    assert not any(c[0] in fake.rules and c[1] == "-F" for c in fake.calls)


def test_a_lock_closes_the_job_user_entirely_and_reopens(cloud_runner, fake):
    cloud_runner.job_rules(999, locked=True, run=fake.run)
    own = "-m owner --uid-owner 999"
    for tool in ("iptables", "ip6tables"):
        chain = [_flat(r) for r in fake.rules[tool] if r[0] == "CANOPY-JOB"]
        assert chain[0] == f"CANOPY-JOB {own} -j REJECT"              # first, ahead of the DNS allow
    cloud_runner.job_rules(999, locked=False, run=fake.run)
    assert not any(_flat(r) == f"CANOPY-JOB {own} -j REJECT" for r in fake.rules["iptables"])


def test_a_lock_set_in_canopy_web_reaches_the_job_user(cloud_runner, monkeypatch, tmp_path):
    """`egress_rules` names each runner's uid, and canopy-job is not a runner, so the
    every-minute sync has to carry a lock to it as well."""
    seen = []
    monkeypatch.setattr(cloud_runner, "_sync_job_rules", lambda locked, run: seen.append(locked))
    monkeypatch.setattr(cloud_runner, "egress_rules", lambda *a, **k: [])
    monkeypatch.setattr(cloud_runner, "_install_unit_file", lambda *a, **k: False)
    runners = [{"id": "dimagi", "uid": os.getuid(), "gid": os.getgid(), "name": "b", "user": "u", "home": str(tmp_path),
                "kind": "tree", "workspace": "w", "slug": ""}]
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    box = {"CANOPY_BASE_URL": "https://x", "RUNNER_NAME": "b"}
    for locked in (True, False):
        cloud_runner.apply_egress(box, runners, boot=True, run=run, etc=tmp_path / "etc",
                                  chown=lambda *a: None,
                                  fetch=lambda on=locked: {"b": {"locked": on, "allow": []}})
    assert seen == [True, False]


def test_template_creates_user_rule_and_metadata_block():
    """The box's half, read from the text: it reaches a box through `--install-runners`
    (cloud-init) and `--setup-runners` (every boot and every idle update)."""
    source = (EC2 / "cloud_runner.py").read_text()
    template = (EC2 / "runner.cfn.yaml").read_text()
    assert "/opt/canopy-runner/cloud_runner.py, --install-runners" in template
    assert "python3-yaml" in template
    setup = source[source.index('if argv[0] == "--setup-runners":'):source.index("return pair_runners_main()")]
    assert "imds_rules()" in setup and setup.index("imds_rules()") < setup.index("setup_jobs(")
    # the broker only on a box whose rules held
    assert setup.index("setup_jobs(") < setup.index("install_job_broker()")
    assert '"disable", "--now", "canopy-job-broker.service"' in setup
    body = source[source.index("def setup_jobs("):source.index("def _sync_job_rules(")]
    assert '"useradd", "--system"' in body and "job_rules(" in body and '"/nonexistent"' in body
    assert 'IMDS_V4, IMDS_V6 = "169.254.169.254", "fd00:ec2::254"' in source
    # iptables doesn't survive a reboot: the unit that makes these runs on every boot
    unit = source[source.index('"canopy-runners.service": f"""'):source.index('"canopy-kms-broker.service"')]
    assert "--setup-runners" in unit and "WantedBy=multi-user.target" in unit


def test_no_sudo_left_in_the_job_path():
    source = (EC2 / "cloud_runner.py").read_text()
    section = source[source.index("# ── plain jobs: read a job's definition"):source.index("def _repo_worktree_cli(")]
    for word in ('"sudo"', "pkill", "pgrep", "_as_job_user"):
        assert word not in section, word
    assert "sudoers" not in source[source.index("def setup_jobs("):source.index("def _sync_job_rules(")]


def test_runner_units_keep_no_new_privileges(cloud_runner):
    """The reason there is a broker at all: model turns stay unable to gain privilege."""
    assert "NoNewPrivileges=yes" in cloud_runner.SYSTEMD_UNITS["canopy-runner@.service"]


SELF_CHECK_ON_A_BOX = "/opt/canopy-runner/job-broker/job_self_check.sh"


def test_the_self_check_is_in_every_sha_path_list():
    """A check that changes alone must reach a box, like the broker it checks."""
    root = EC2.parent.parent
    assert '"runner/ec2/job_self_check.sh"' in (EC2 / "update_runner.sh").read_text().split("SHA_PATHS=(", 1)[1].split(")", 1)[0]
    assert " job_self_check.sh " in (EC2 / "up.sh").read_text().split("SHA_PATHS=(", 1)[1].split(")", 1)[0] + " "
    workflow = (root / ".github" / "workflows" / "deploy-labs.yml").read_text()
    paths = next(ln for ln in workflow.splitlines() if ln.strip().startswith('PATHS="'))
    assert "runner/ec2/job_self_check.sh" in paths


def test_every_doc_runs_the_self_check_where_it_is_installed():
    root = EC2.parent.parent
    for doc in (root / "docs/architecture/deploy.md", EC2 / "README.md", EC2 / "job_self_check.sh"):
        text = doc.read_text()
        assert f"sudo bash {SELF_CHECK_ON_A_BOX}" in text, doc
        assert "/opt/canopy-web/runner/ec2/job_self_check.sh" not in text, doc


def test_the_broker_install_puts_the_self_check_beside_it(runner, monkeypatch, tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(runner, "CANOPY_WEB_REPO_DIR", str(tmp_path / "repo"))
    monkeypatch.setattr(runner, "JOB_BROKER_DIR", tmp_path / "job-broker")
    monkeypatch.setattr(runner, "build_info", lambda: {"sha": "abc"})
    shown = []

    def run(argv, **kw):
        if "show" in argv:
            shown.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=f"# {argv[-1]}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert runner.install_job_broker(run=run) is True
    # The same sha as the broker, never whatever the clone has checked out.
    assert shown == ["abc:runner/ec2/job_broker.py", "abc:runner/ec2/job_self_check.sh"]
    check = tmp_path / "job-broker" / "job_self_check.sh"
    assert check.read_text() == "# abc:runner/ec2/job_self_check.sh\n"
    assert check.stat().st_mode & 0o777 == 0o755


def test_a_sha_without_the_self_check_still_installs_the_broker(runner, monkeypatch, tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(runner, "CANOPY_WEB_REPO_DIR", str(tmp_path / "repo"))
    monkeypatch.setattr(runner, "JOB_BROKER_DIR", tmp_path / "job-broker")
    monkeypatch.setattr(runner, "build_info", lambda: {"sha": "abc"})
    monkeypatch.setattr(runner, "_log", lambda *a, **k: None)

    def run(argv, **kw):
        missing = "show" in argv and argv[-1].endswith("job_self_check.sh")
        return subprocess.CompletedProcess(argv, 128 if missing else 0, stdout="" if missing else "# x\n", stderr="")

    assert runner.install_job_broker(run=run) is True
    assert (tmp_path / "job-broker" / "job_broker.py").exists()


def test_the_broker_ships_as_the_runner_does():
    """In the sha the updater and the deploy compute, or its change would never reach a box."""
    assert '"runner/ec2/job_broker.py"' in (EC2 / "update_runner.sh").read_text()


@pytest.mark.parametrize("name", ["kms_broker", "aws_broker"])
def test_broker_refuses_other_users(tmp_path, name):
    """canopy-job's uid isn't in runners.json, so each broker answers with an error and makes
    no KMS or STS call. Both sockets are 0666 and this is their whole check. (A job can't even
    open one: the unit has RestrictAddressFamilies and InaccessiblePaths.)"""
    spec = importlib.util.spec_from_file_location(name, EC2 / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    path = tmp_path / "runners.json"
    path.write_text(json.dumps({"runners": [{"uid": 2001, "id": "dimagi", "kms_key": "alias/x"}]}))
    job_uid = 999
    if name == "kms_broker":
        keys = mod.load_keys(str(path))
        assert job_uid not in keys and 2001 in keys
        boom = SimpleNamespace(public_key=lambda *a: pytest.fail("KMS was called"),
                               derive=lambda *a: pytest.fail("KMS was called"))
        reply = mod.answer(b'{"op":"public_key"}', job_uid, keys, boom)
    else:
        runners = mod.load_runners(str(path))
        assert job_uid not in runners and 2001 in runners
        boom = SimpleNamespace(role_arn=lambda *a: pytest.fail("STS was called"),
                               assume=lambda *a, **k: pytest.fail("STS was called"))
        reply = mod.answer(b'{"role":"labs-observe","agent":"eva"}', job_uid, runners, boom, mod.Cache())
    assert "not a runner" in reply["error"], name


def test_self_check_covers_each_thing_and_is_clean_shell():
    path = EC2 / "job_self_check.sh"
    text = path.read_text()
    assert os.access(path, os.X_OK)
    subprocess.run(["bash", "-n", str(path)], check=True)
    for needle in ("169.254.169.254", "fd00:ec2::254", "/run/canopy-kms/sock", "/run/canopy-aws/sock",
                   "/run/canopy-jobs/sock", "runner-token.json", ".canopy-cloud-runner.json", ".gog-secret",
                   "box.env", ".canopy", "/.env", "/home", "/dev/shm", "memory.max", "pids.max", "TasksMax",
                   "NoNewPrivileges", "not a runner", "127.0.0.1", "::1", "getaddrinfo", "/run/canopy/",
                   '"start"', '"export"', '"cleanup"', "PASS", "FAIL", "sibling", "48731", "expected", "/proc/", "/root", "missing",
                   "by the next job's start"):
        assert needle in text, needle
    if shutil.which("shellcheck"):
        res = subprocess.run(["shellcheck", str(path)], capture_output=True, text=True)
        assert res.returncode == 0, res.stdout


def _self_check_part(text, name):
    return re.search(r"<<'PY'\n(.*?)\nPY\n", text[text.index(f'cat > "$WORK/{name}"'):], re.S).group(1)


def test_self_check_counts_what_it_says_it_counts(tmp_path, monkeypatch):
    """The script compares PASS + FAIL to a count, so a crash after a first PASS can't read as
    "all passed". This runs its real client and probe against the harness broker and counts:
    the verdicts are meaningless here, the number of them isn't."""
    text = (EC2 / "job_self_check.sh").read_text()
    work = tmp_path / "work"
    work.mkdir()
    for name in ("probe.py", "client.py"):
        (work / name).write_text(_self_check_part(text, name))
    (work / "memory.py").write_text("raise SystemExit(1)\n")   # 3 GiB isn't this test's business
    (work / "secret").write_text("x")
    (work / "refused.txt").write_text(f"{work}/secret\n/nonexistent\n")
    (work / "sockets.txt").write_text("/nonexistent/a\n/nonexistent/b\n/nonexistent/c\n")
    (work / "closed-targets.txt").write_text("nothing here|127.0.0.1|9\nnor here|127.0.0.1|19\n")
    broker = load_broker()
    monkeypatch.setattr(broker, "JOB_PATH", f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin")
    root = tmp_path / "jobs"
    root.mkdir()
    marker = f"MARK{tmp_path.name}"
    harness = Harness(broker, root, marker)
    try:
        res = subprocess.run([sys.executable, str(work / "client.py"), harness.path, str(work), "no-such-user"],
                             capture_output=True, text=True, timeout=120, cwd="/",
                             env={**os.environ, "SELF_CHECK_EXTRA": marker})
    finally:
        harness.close()
    verdicts = [ln for ln in res.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    fixed = int(re.search(r"^CLIENT_CHECKS=(\d+)$", text, re.M).group(1))
    probe = int(re.search(r"^PROBE_CHECKS=(\d+)$", text, re.M).group(1))
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(verdicts) == fixed + probe + 2 + 3 + 2, res.stdout


def test_self_check_only_probes_a_path_root_has_seen_there():
    """Not there, it reads ENOENT inside the unit, which proves nothing. The script's filter runs
    here on real paths: what's missing is printed as a SKIP by name and counted."""
    text = (EC2 / "job_self_check.sh").read_text()
    body = re.search(r"^filter_existing\(\) \{.*?^\}$", text, re.M | re.S).group(0)
    script = ("set -uo pipefail\nskip() { printf 'SKIP  %s\\n' \"$1\"; SKIPPED=$((SKIPPED + 1)); }\nSKIPPED=0\n"
              + body + '\nfilter_existing < <(printf "%s\\n" /bin /no/such/path)\necho "n=$SKIPPED"\n')
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert res.stdout.split() == ["/bin", "n=1"]
    assert "/no/such/path" in res.stderr


@pytest.mark.parametrize("needle", [
    'rc=$?', 'expected', "can't establish", "listen_py", "SKIPPED",
])
def test_self_check_fails_on_what_it_cant_establish(needle):
    assert needle in (EC2 / "job_self_check.sh").read_text()


def test_runner_starts_after_the_job_broker(cloud_runner):
    assert "canopy-job-broker.service" in cloud_runner.SYSTEMD_UNITS["canopy-runner@.service"].split("Wants=")[0]


def test_readme_says_what_a_job_cannot_be_kept_from():
    readme = (EC2 / "README.md").read_text()
    section = readme[readme.index("## Plain jobs"):readme.index("## Several runners on one machine")]
    for needle in ("job_self_check.sh", "Base64", "NoNewPrivileges", "Manual check", "job_broker.py",
                   "isn't a boundary", "their own runner", "/nonexistent"):
        assert needle in section, needle
