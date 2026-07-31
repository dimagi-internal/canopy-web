"""The updater's decision procedure, driven for real.

`update_runner.sh` is a linear bash program whose whole content is WHICH commands
run against WHAT — invisible to any amount of reading it. So these run the real
script against a real git repo, with `curl` and the restart command faked on PATH,
and assert on the calls it makes.

The four verdicts mirror the laptop's `update.py` exactly (current | stale | busy |
unknown), and the two that look like edge cases are the load-bearing ones: a stale
busy-marker must NOT read as busy (that box is the one auto-update exists to
rescue), and either sha being empty must read as unknown (installing an empty sha
would be a reinstall loop against a target that does not exist).

See docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "update_runner.sh"
RUNNER_ID = "11111111-2222-3333-4444-555555555555"
GOOD_RUNNER = "#!/usr/bin/env python3\nprint('i am the new runner')\n"


def _git(repo: pathlib.Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class Box:
    """A fake cloud box: runner home, a canopy-web clone, and stubbed commands."""

    def __init__(self, tmp_path: pathlib.Path):
        self.root = tmp_path
        self.home = tmp_path / "opt" / "canopy-runner"
        self.home.mkdir(parents=True)
        self.repo = tmp_path / "opt" / "canopy-web"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.home / "runner.env").write_text(
            "CANOPY_BASE_URL=https://labs.example/canopy\nCANOPY_TOKEN=tok\n"
        )
        (tmp_path / "state.json").write_text(json.dumps({"runner_id": RUNNER_ID}))
        self.curl_log = tmp_path / "curl.log"
        self.restart_log = tmp_path / "restart.log"
        self.api_body = tmp_path / "api.json"
        self.api_body.write_text("[]")
        self._write_stub("curl", f'echo "$@" >> "{self.curl_log}"\ncat "{self.api_body}"\n')
        self._write_stub("fake-restart", f'echo "$@" >> "{self.restart_log}"\n')
        self.seed_repo(GOOD_RUNNER)

    def _write_stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    # -- setup helpers --
    def seed_repo(self, runner_source: str) -> str:
        """Commit `runner_source` as runner/ec2/cloud_runner.py; return the sha.

        `--allow-empty` so re-seeding identical content still advances HEAD, and
        the sha returned is PATH-SCOPED — the same `git log -1 -- <paths>` the
        script and deploy-labs.yml both run. That is the point of the whole
        scheme: HEAD moves on every canopy-web commit, this does not.
        """
        first = not self.repo.exists()
        src = self.repo / "runner" / "ec2"
        src.mkdir(parents=True, exist_ok=True)
        (src / "cloud_runner.py").write_text(runner_source)
        (src / "bootstrap_agents.sh").write_text("#!/bin/bash\n")
        if first:
            _git(self.repo.parent, "init", "-q", "canopy-web")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "runner")
        return _git(self.repo, "log", "-1", "--format=%H", "--",
                    "runner/ec2/cloud_runner.py", "runner/ec2/bootstrap_agents.sh")

    def stamp(self, sha: str, committed_at: int = 1) -> None:
        (self.home / "build-info.json").write_text(
            json.dumps({"sha": sha, "committed_at": committed_at})
        )

    def expect(self, sha: str) -> None:
        """What the control plane says this runner SHOULD be running."""
        self.api_body.write_text(json.dumps(
            [{"id": RUNNER_ID, "name": "cloud-ec2-1", "expected_code_sha": sha}]
        ))

    def busy(self, count: int, age_seconds: float = 0.0) -> None:
        (self.home / "in-flight").write_text(
            json.dumps({"count": count, "at": time.time() - age_seconds})
        )

    @property
    def installed(self) -> str:
        target = self.home / "cloud_runner.py"
        return target.read_text() if target.exists() else ""

    @property
    def restarted(self) -> bool:
        return self.restart_log.exists()

    # -- run it --
    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.root),
                "RUNNER_HOME": str(self.home),
                "CANOPY_WEB_REPO_DIR": str(self.repo),
                "STATE_FILE": str(self.root / "state.json"),
                "RESTART_CMD": str(self.bin / "fake-restart"),
            },
        )


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# --- the four verdicts ------------------------------------------------------
def test_current_when_the_stamp_matches_the_deployed_sha(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp(sha)
    box.expect(sha)
    out = box.run("--check")
    assert out.stdout.startswith("current"), out.stdout
    assert out.returncode == 0


def test_stale_when_they_differ_and_nothing_is_in_flight(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp("0" * 40)
    box.expect(sha)
    assert box.run("--check").stdout.startswith("stale")


def test_busy_while_a_turn_is_in_flight(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp("0" * 40)
    box.expect(sha)
    box.busy(count=1)
    assert box.run("--check").stdout.startswith("busy")


def test_a_stale_marker_does_not_read_as_busy(box):
    # THE case auto-update exists for: a daemon that stopped writing the marker is
    # stopped, wedged or crash-looping — not busy. Treating it as busy would leave
    # exactly the broken boxes un-rescuable, forever.
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp("0" * 40)
    box.expect(sha)
    box.busy(count=4, age_seconds=600)
    assert box.run("--check").stdout.startswith("stale")


def test_unknown_when_the_box_has_no_stamp(box):
    box.expect(box.seed_repo(GOOD_RUNNER))
    assert box.run("--check").stdout.startswith("unknown")


def test_unknown_when_the_server_expects_nothing(box):
    # A dev image bakes in no expectation. Empty means UNKNOWN, never "different".
    box.stamp("abc123")
    box.expect("")
    assert box.run("--check").stdout.startswith("unknown")


def test_unknown_when_this_runner_is_not_in_the_fleet_list(box):
    # Retired, or invisible to this token. Reinstalling would not fix either.
    box.stamp("abc123")
    box.api_body.write_text(json.dumps([{"id": "someone-else", "expected_code_sha": "x" * 40}]))
    assert box.run("--check").stdout.startswith("unknown")


def test_unknown_when_the_control_plane_is_unreachable(box):
    box.stamp("abc123")
    box._write_stub("curl", "exit 7\n")
    out = box.run("--check")
    assert out.stdout.startswith("unknown")
    assert out.returncode == 0


# --- installing -------------------------------------------------------------
def test_stale_and_idle_installs_the_expected_sha_and_restarts(box):
    box.stamp("0" * 40)
    sha = box.seed_repo("#!/usr/bin/env python3\nprint('shipped')\n")
    box.expect(sha)

    out = box.run()
    assert out.returncode == 0, out.stderr
    assert "print('shipped')" in box.installed
    assert box.restarted
    assert "canopy-runner.service" in box.restart_log.read_text()

    stamped = json.loads((box.home / "build-info.json").read_text())
    assert stamped["sha"] == sha
    assert stamped["ref"] == sha
    # Re-stamped means the very next check reads `current` — without it the box
    # would reinstall and restart every 30 minutes, forever.
    assert box.run("--check").stdout.startswith("current")


def test_current_installs_nothing(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp(sha)
    box.expect(sha)
    box.run()
    assert box.installed == ""
    assert not box.restarted


def test_busy_installs_nothing(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp("0" * 40)
    box.expect(sha)
    box.busy(count=2)
    box.run()
    assert box.installed == ""
    assert not box.restarted


def test_it_refuses_to_install_a_file_that_does_not_parse(box):
    # systemd's ExecStart points straight at this file: a truncated or broken copy
    # is a box that will not boot. Better to stay stale than to install that.
    box.stamp("0" * 40)
    (box.home / "cloud_runner.py").write_text("# the working runner\n")
    sha = box.seed_repo("def broken(:\n  syntax error\n")
    box.expect(sha)

    out = box.run()
    assert "REFUSING" in out.stdout
    assert box.installed == "# the working runner\n"
    assert not box.restarted
    assert not (box.home / "cloud_runner.py.new").exists()


def test_a_sha_not_yet_in_the_clone_is_retried_not_failed(box):
    # The deploy can be ahead of this box's clone. Survivable and self-correcting:
    # log it and wait for the next cycle rather than shouting every 30 minutes.
    box.stamp("0" * 40)
    box.expect("f" * 40)
    out = box.run()
    assert "will retry" in out.stdout
    assert not box.restarted


# --- the read-only rule -----------------------------------------------------
def test_the_updater_never_writes_to_the_control_plane(box):
    # A heartbeat from this second process would stamp the runner ONLINE and
    # overwrite the provenance the real daemon reports — forging liveness for a
    # daemon that may be dead. It must only ever GET.
    box.stamp("0" * 40)
    box.expect(box.seed_repo(GOOD_RUNNER))
    box.run()
    calls = box.curl_log.read_text()
    assert "runners/" in calls
    assert "-X POST" not in calls and "--data" not in calls and "-d " not in calls
    assert "heartbeat" not in calls


# --- deliberate overrides ---------------------------------------------------
def test_ref_installs_something_else_on_purpose(box):
    sha = box.seed_repo("#!/usr/bin/env python3\nprint('branch build')\n")
    box.stamp("0" * 40)
    box.expect("0" * 40)  # the server thinks this box is current
    out = box.run("--ref", sha)
    assert out.returncode == 0, out.stderr
    assert "print('branch build')" in box.installed
    assert box.restarted
