"""The box reads a plain job's definition and exports its repo (Part 3, Task 2).

Nothing here runs a job. `_as_job_user` is the one seam that stands for
`sudo -n -u canopy-job`; the tests run its command as the current user.
"""
from __future__ import annotations

import io
import subprocess
import tarfile

import pytest


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
    calls = []

    def as_job_user(args, input=None):
        calls.append(list(args))
        return subprocess.run(list(args), input=input, capture_output=True)

    monkeypatch.setattr(cloud_runner, "_as_job_user", as_job_user)
    cloud_runner._seam_calls = calls
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


def test_spec_bad_job_name_refused(runner):
    with pytest.raises(runner.JobRefused):
        runner._job_spec(GOOD, "Sync; rm")


def test_spec_read_from_export(runner):
    export = runner._job_export("tools", "turn-1", None)
    assert runner._job_spec_for(export, "sync").run == ["./sync.sh"]
    with pytest.raises(runner.JobRefused):
        runner._job_spec_for(export, "absent")


def test_spec_missing_file_sentence(runner, origins):
    repo = origins / "tools"
    _git("rm", "-rq", ".canopy", cwd=repo)
    _commit(repo, "no jobs")
    export = runner._job_export("tools", "turn-2", None)
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_spec_for(export, "sync")
    assert ".canopy/jobs.yaml" in str(exc.value)


# ── _job_export ──────────────────────────────────────────────────────────────

def test_export_uses_main_not_a_branch(runner, origins):
    repo = origins / "tools"
    _git("checkout", "-q", "-b", "feature", cwd=repo)       # origin's HEAD is now feature
    (repo / "README.md").write_text("feature\n")
    _commit(repo, "feature")
    export = runner._job_export("tools", "turn-1", None)
    assert (export / "README.md").read_text() == "main v1\n"
    # and a stale clone is brought forward: main moves on, a new job sees it
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "README.md").write_text("main v2\n")
    _commit(repo, "v2")
    assert (runner._job_export("tools", "turn-2", None) / "README.md").read_text() == "main v2\n"


def test_export_has_no_git_dir(runner):
    export = runner._job_export("tools", "turn-1", None)
    assert export == runner.JOBS_ROOT / "turn-1" / "repo"
    assert not list(export.rglob(".git*"))
    assert (export / ".canopy" / "jobs.yaml").is_file()
    # unpacked by the job user, without owner or permission bits from the archive
    unpack = [c for c in runner._seam_calls if c[0] == "tar"]
    assert len(unpack) == 1
    assert "--no-same-owner" in unpack[0] and "--no-same-permissions" in unpack[0]


def test_export_leaves_no_ref_behind(runner):
    runner._job_export("tools", "turn-1", None)
    clone = runner._repos_root() / ".clones" / "tools.git"
    refs = _git("for-each-ref", "refs/jobs", cwd=clone)
    assert refs == ""


def test_failed_fetch_refuses_never_stale(runner, origins):
    runner._job_export("tools", "turn-1", None)               # clone exists now
    (origins / "tools").rename(origins / "gone")              # origin unreachable
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_export("tools", "turn-2", None)
    assert "latest" in str(exc.value)
    assert not (runner.JOBS_ROOT / "turn-2").exists()


def test_failed_first_clone_refuses(runner):
    with pytest.raises(runner.JobRefused):
        runner._job_export("missing-repo", "turn-1", None)
    assert not (runner.JOBS_ROOT / "turn-1").exists()


def test_missing_main_refuses(runner, origins):
    repo = origins / "tools"
    _git("branch", "-m", "main", "trunk", cwd=repo)
    with pytest.raises(runner.JobRefused):
        runner._job_export("tools", "turn-1", None)


@pytest.mark.parametrize("repo", ["a/b", "..", ".", "", "x" * 101, "-rf", "a b", "../x", "x.git",
                                  "https://evil/x", "a;b"])
def test_bad_repo_name_never_reaches_git(runner, monkeypatch, repo):
    def boom(*a, **k):
        raise AssertionError("git or a path was touched")

    monkeypatch.setattr(runner, "_repo_clone", boom)
    monkeypatch.setattr(runner.subprocess, "run", boom)
    monkeypatch.setattr(runner.subprocess, "Popen", boom)
    monkeypatch.setattr(runner, "_as_job_user", boom)
    with pytest.raises(runner.JobRefused):
        runner._job_export(repo, "turn-1", None)


@pytest.mark.parametrize("turn_id", ["", "../x", "a/b", ".", "..", "x" * 65, "a b"])
def test_bad_turn_id_never_becomes_a_path(runner, monkeypatch, turn_id):
    monkeypatch.setattr(runner, "_as_job_user", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(runner.JobRefused):
        runner._job_export("tools", turn_id, None)


def test_export_refuses_a_symlink_pointing_out(runner, origins):
    repo = origins / "tools"
    (repo / "escape").symlink_to("/etc")
    _git("add", "-A", cwd=repo)
    _commit(repo, "link")
    with pytest.raises(runner.JobRefused) as exc:
        runner._job_export("tools", "turn-1", None)
    assert "link" in str(exc.value)
    assert not (runner.JOBS_ROOT / "turn-1").exists()


def test_export_keeps_a_symlink_within_the_checkout(runner, origins):
    repo = origins / "tools"
    (repo / "latest.md").symlink_to("README.md")
    _git("add", "-A", cwd=repo)
    _commit(repo, "inner link")
    export = runner._job_export("tools", "turn-1", None)
    assert (export / "latest.md").is_symlink()
    assert (export / "latest.md").read_text() == "main v1\n"


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


def test_export_cleans_up_when_unpack_fails(runner, monkeypatch):
    real = runner._as_job_user

    def flaky(args, input=None):
        if args[0] == "tar":
            return subprocess.CompletedProcess(args, 2, b"", b"boom")
        return real(args, input=input)

    monkeypatch.setattr(runner, "_as_job_user", flaky)
    with pytest.raises(runner.JobRefused):
        runner._job_export("tools", "turn-1", None)
    assert not (runner.JOBS_ROOT / "turn-1").exists()


# ── the capability ───────────────────────────────────────────────────────────

@pytest.fixture
def can(runner, monkeypatch):
    monkeypatch.setattr(runner, "_job_user_exists", lambda: True)
    monkeypatch.setattr(runner, "JOBS_ENABLED", True)
    monkeypatch.setattr(runner, "_as_job_user",
                        lambda args, input=None: subprocess.CompletedProcess(args, 0, b"", b""))
    runner.RUNNER_CAPS.pop("jobs", None)
    return runner


def test_caps_report_jobs_only_when_able(can, monkeypatch):
    can._apply_job_cap()
    assert can.RUNNER_CAPS["jobs"] is True

    monkeypatch.setattr(can, "_job_user_exists", lambda: False)
    can._apply_job_cap()
    assert can.RUNNER_CAPS.get("jobs") is not True

    monkeypatch.setattr(can, "_job_user_exists", lambda: True)
    monkeypatch.setattr(can, "_as_job_user",
                        lambda args, input=None: subprocess.CompletedProcess(args, 1, b"", b"no sudo"))
    can._apply_job_cap()
    assert can.RUNNER_CAPS.get("jobs") is not True

    def explode(args, input=None):
        raise OSError("sudo missing")

    monkeypatch.setattr(can, "_as_job_user", explode)
    assert can._job_can_run() is False


def test_sensitive_box_never_reports_jobs(can, monkeypatch):
    monkeypatch.setattr(can, "SENSITIVE_DATA_MODE", True)
    assert can._job_can_run() is False
    can._apply_job_cap()
    assert can.RUNNER_CAPS.get("jobs") is not True
    monkeypatch.setattr(can, "SENSITIVE_DATA_MODE", False)
    monkeypatch.setattr(can, "RUNNER_DATA_MODE", "sensitive_data")
    assert can._job_can_run() is False


def test_caps_off_while_the_switch_is_off(can, monkeypatch):
    """Until Task 3 turns JOBS_ENABLED on, an able box still says nothing."""
    monkeypatch.setattr(can, "JOBS_ENABLED", False)
    can._apply_job_cap()
    assert "jobs" not in can.RUNNER_CAPS


def test_switch_ships_off(cloud_runner):
    assert cloud_runner.JOBS_ENABLED is False
    assert "jobs" not in cloud_runner.RUNNER_CAPS
