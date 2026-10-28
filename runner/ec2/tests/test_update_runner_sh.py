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
import re
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
        (tmp_path / "etc-needrestart" / "conf.d").mkdir(parents=True)
        self.needrestart_conf = tmp_path / "etc-needrestart" / "conf.d" / "50-canopy-runner.conf"
        (tmp_path / "etc-tmpfiles").mkdir()
        self.tmpfiles_conf = tmp_path / "etc-tmpfiles" / "tmp.conf"
        # The pre-several-runners user's home, the volume's fill and its processes: stubbed,
        # so a test never measures, or deletes from, the machine it runs on.
        self.legacy_home = tmp_path / "legacy-home"
        self.legacy_home.mkdir()
        self.disk_pct = tmp_path / "disk-pct"
        self.disk_pct.write_text("10")
        self._write_stub("df", f'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
                               f'echo "/dev/root 100 10 90 $(cat "{self.disk_pct}")% /"\n')
        self.legacy_busy = tmp_path / "legacy-busy"
        self._write_stub("pgrep", f'[ -e "{self.legacy_busy}" ]\n')
        self.seed_repo(GOOD_RUNNER)

    def env(self) -> dict:
        """What every run sets beside its own variables: the root-level files go here."""
        return {"NEEDRESTART_CONF": str(self.needrestart_conf), "PRIV_CMD": "",
                "TMPFILES_CONF": str(self.tmpfiles_conf), "LEGACY_HOME": str(self.legacy_home),
                "DISK_USAGE_ROOT": str(self.root)}

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
        (src / "update_runner.sh").write_text("#!/bin/bash\n")
        if first:
            _git(self.repo.parent, "init", "-q", "canopy-web")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "runner")
        return _git(self.repo, "log", "-1", "--format=%H", "--",
                    "runner/ec2/cloud_runner.py", "runner/ec2/bootstrap_agents.sh",
                    "runner/ec2/update_runner.sh")

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
                **self.env(),
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


# --- the credentials refresh runs under the RUNNER'S env ----------------------
#
# canopy-runner.service loads runner.env; canopy-runner-update.service does not.
# So the --credentials-only pass — whose whole purpose is to pick up a credential
# set in canopy-web — ran with no GOG_KEYRING_PASSWORD and no CANOPY_TOKEN, saw
# every mailbox as dead, and re-reported that to the control plane every 30 min
# (cloud-ec2-1, 2026-09-10 11:59Z: "no gog token anywhere" for ace, seven seconds
# before the service start read ace's vault AND its canopy-web token).
def test_the_credentials_refresh_sees_the_runners_env(box):
    (box.home / "runner.env").write_text(
        "CANOPY_BASE_URL=https://labs.example/canopy\nCANOPY_TOKEN=tok\n"
        "GOG_KEYRING_PASSWORD=kp\n"
    )
    sha = box.seed_repo(GOOD_RUNNER)   # seed FIRST — it rewrites bootstrap_agents.sh
    seen = box.root / "bootstrap-env.txt"
    stub = box.repo / "runner" / "ec2" / "bootstrap_agents.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "args=$*" "CANOPY_TOKEN=${{CANOPY_TOKEN:-}}" '
        f'"GOG_KEYRING_PASSWORD=${{GOG_KEYRING_PASSWORD:-}}" > "{seen}"\n'
    )
    stub.chmod(0o755)
    box.stamp(sha)
    box.expect(sha)          # current → nothing to install, refresh still runs
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert seen.exists(), "the credentials-only pass did not run"
    lines = seen.read_text().splitlines()
    assert "args=--credentials-only" in lines
    assert "CANOPY_TOKEN=tok" in lines, "no token → no vault config, no canopy-web token"
    assert "GOG_KEYRING_PASSWORD=kp" in lines, "no keyring password → every mailbox reads as dead"


def test_a_moved_box_calls_its_new_address_not_runner_envs(box):
    # runner.env is re-rendered from the stack parameter (the OLD address) on every
    # start; the runner records a move in base_url.override. The timer must read
    # the override too, or it calls the address canopy has left every 30 minutes.
    (box.home / "base_url.override").write_text("https://new.example\n")
    sha = box.seed_repo(GOOD_RUNNER)
    seen = box.root / "bootstrap-env.txt"
    stub = box.repo / "runner" / "ec2" / "bootstrap_agents.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "CANOPY_BASE_URL=${{CANOPY_BASE_URL:-}}" '
        f'"CANOPY_WEB_API_URL=${{CANOPY_WEB_API_URL:-}}" > "{seen}"\n'
    )
    stub.chmod(0o755)
    box.stamp(sha)
    box.expect(sha)
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "https://new.example/api/harness/runners/" in box.curl_log.read_text()
    assert "labs.example" not in box.curl_log.read_text()
    lines = seen.read_text().splitlines()
    assert "CANOPY_BASE_URL=https://new.example" in lines
    assert "CANOPY_WEB_API_URL=https://new.example" in lines


# 2026-10-17: runner.env's CANOPY_TOKEN (a person's PAT) was revoked. Every
# expected_sha read came back empty — "unknown", so the box stopped updating — and
# the refresh's roster read was refused, so only the AGENT_SLUGS fallback was ever
# refreshed. The box's own runner token (shared by cloud_runner.py) comes first.
def _share_token(box, token: str, expires_in: float) -> None:
    (box.home / "runner-token.json").write_text(
        json.dumps({"access_token": token, "expires_at": time.time() + expires_in}))


def _record_refresh_token(box) -> pathlib.Path:
    seen = box.root / "bootstrap-env.txt"
    stub = box.repo / "runner" / "ec2" / "bootstrap_agents.sh"
    stub.write_text("#!/bin/bash\n"
                    f'printf "%s\\n" "CANOPY_TOKEN=${{CANOPY_TOKEN:-}}" > "{seen}"\n')
    stub.chmod(0o755)
    return seen


def test_the_updater_reads_as_the_box_when_it_has_a_runner_token(box):
    sha = box.seed_repo(GOOD_RUNNER)
    seen = _record_refresh_token(box)
    box.stamp(sha)
    box.expect(sha)
    _share_token(box, "crt_box", 3600)
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "Bearer crt_box" in box.curl_log.read_text()
    assert "Bearer tok" not in box.curl_log.read_text()
    assert "CANOPY_TOKEN=crt_box" in seen.read_text().splitlines()


def test_an_expired_runner_token_falls_back_to_runner_envs(box):
    sha = box.seed_repo(GOOD_RUNNER)
    seen = _record_refresh_token(box)
    box.stamp(sha)
    box.expect(sha)
    _share_token(box, "crt_old", 30)   # inside the 120 s margin: treated as gone
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "Bearer tok" in box.curl_log.read_text()
    assert "CANOPY_TOKEN=tok" in seen.read_text().splitlines()


def test_an_override_that_is_not_https_is_ignored(box):
    (box.home / "base_url.override").write_text("http://evil.example\n")
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp(sha)
    box.expect(sha)
    box.run("--check")
    assert "https://labs.example/canopy/api/harness/runners/" in box.curl_log.read_text()


def test_every_run_moves_origin_main_so_the_shim_runs_the_newest_updater(box):
    # fetch_commit fetches the deployed commit BY SHA, which leaves origin/main
    # where it was — and origin/main is what the shim runs. cloud-ec2-2 ran a
    # day-old updater because of it (2026-10-06).
    upstream = box.root / "upstream"
    subprocess.run(["git", "clone", "-q", str(box.repo), str(upstream)], check=True)
    _git(upstream, "checkout", "-q", "-B", "main")  # CI's git may default to master
    _git(box.repo, "remote", "add", "origin", str(upstream))
    _git(box.repo, "fetch", "-q", "origin")
    (upstream / "marker").write_text("new\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "newer main")
    newest = _git(upstream, "rev-parse", "HEAD")
    box.run()
    assert _git(box.repo, "rev-parse", "origin/main") == newest


# --- the updater itself ships by deploy -------------------------------------
#
# The shim reads update_runner.sh from the clone's origin/main WITHOUT fetching;
# origin/main only moves when the install path runs, and that only happens when a
# SHA_PATHS file changes. With update_runner.sh absent from the list, an updater-
# only change never reached a box (#742: merged, and the next tick still ran the
# old script). The two lists — here and in deploy-labs.yml — must agree, and both
# must name the updater.
def test_the_updater_is_in_both_sha_path_lists_and_they_agree():
    script = SCRIPT.read_text()
    m = re.search(r'^SHA_PATHS=\((.*)\)$', script, re.M)
    assert m, "SHA_PATHS not found in update_runner.sh"
    script_paths = re.findall(r'"([^"]+)"', m.group(1))
    workflow = (SCRIPT.parent.parent.parent / ".github" / "workflows" / "deploy-labs.yml").read_text()
    m2 = re.search(r'^\s*PATHS="([^"]+)"', workflow, re.M)
    assert m2, "cloud_sha PATHS not found in deploy-labs.yml"
    workflow_paths = m2.group(1).split()
    assert script_paths == workflow_paths, "the script and the deploy compute the sha over different files"
    assert "runner/ec2/update_runner.sh" in script_paths, "an updater-only change must be a deploy"


def test_a_change_to_the_updater_alone_moves_the_deployed_sha(box):
    before = box.seed_repo(GOOD_RUNNER)
    (box.repo / "runner" / "ec2" / "update_runner.sh").write_text("#!/bin/bash\n# changed\n")
    _git(box.repo, "add", "-A")
    _git(box.repo, "commit", "-q", "-m", "updater only")
    after = _git(box.repo, "log", "-1", "--format=%H", "--",
                 "runner/ec2/cloud_runner.py", "runner/ec2/bootstrap_agents.sh",
                 "runner/ec2/update_runner.sh")
    assert after != before


# --- the box's own credential (keyless runners) -------------------------------
#
# 2026-10-17: the owner's PAT in runner.env was revoked. The daemon carried on
# with its device-key runner token; this script kept GETting with the PAT, read
# 401 as `unknown`, and installed nothing for hours while the daemon nudged it
# every ten minutes (cloud-ec2-2, expected moving, installed stuck at f6da246).
KEYLESS_RUNNER = (
    "#!/usr/bin/env python3\n"
    "print('import-time noise that must never read as a token')\n"
    "BASE_URL = ''\n"
    "_RUNNER_TOKEN = {'raw': ''}\n"
    "def _log(msg):\n"
    "    print(msg)\n"
    "def _exchange_device_proof(runner_id):\n"
    "    _log('keyless: signed in')\n"
    f"    if runner_id != {RUNNER_ID!r} or not BASE_URL.startswith('https://'):\n"
    "        return False\n"
    "    _RUNNER_TOKEN['raw'] = 'crt_box'\n"
    "    return True\n"
)


def _only_accepts(box, token: str) -> None:
    """canopy-web refusing every bearer but `token` (curl -f exits 22 on a 401)."""
    box._write_stub(
        "curl",
        f'echo "$@" >> "{box.curl_log}"\n'
        f'case "$*" in *"Bearer {token}"*) cat "{box.api_body}" ;; *) exit 22 ;; esac\n',
    )


def test_a_revoked_pat_does_not_stall_a_box_with_an_approved_device_key(box):
    (box.home / "cloud_runner.py").write_text(KEYLESS_RUNNER)
    box.stamp("0" * 40)
    sha = box.seed_repo(GOOD_RUNNER)
    box.expect(sha)
    _only_accepts(box, "crt_box")
    assert box.run("--check").stdout.startswith(f"stale expected={sha}")
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "i am the new runner" in box.installed
    assert box.restarted


def test_no_device_key_falls_back_to_the_pat(box):
    # An installed runner that predates keyless (or a key not yet approved).
    (box.home / "cloud_runner.py").write_text(GOOD_RUNNER)
    box.stamp("abc123")
    box.expect("f" * 40)
    _only_accepts(box, "tok")
    assert box.run("--check").stdout.startswith("stale")


def test_an_unknown_verdict_says_why(box):
    (box.home / "cloud_runner.py").write_text(KEYLESS_RUNNER)
    box.stamp("abc123")
    box.expect("f" * 40)
    _only_accepts(box, "nobody")
    out = box.run("--check")
    assert out.stdout.startswith("unknown")
    assert "unknown because" in out.stderr
    assert "runner token" in out.stderr and "CANOPY_TOKEN" in out.stderr
    assert "crt_box" not in out.stdout + out.stderr, "a token must never reach the journal"


def test_the_credentials_refresh_mints_a_token_when_the_daemon_shared_none(box):
    sha = box.seed_repo(GOOD_RUNNER)
    (box.home / "cloud_runner.py").write_text(KEYLESS_RUNNER)
    seen = box.root / "bootstrap-env.txt"
    stub = box.repo / "runner" / "ec2" / "bootstrap_agents.sh"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "CANOPY_TOKEN=${{CANOPY_TOKEN:-}}" > "{seen}"\n')
    stub.chmod(0o755)
    box.stamp(sha)
    box.expect(sha)
    _only_accepts(box, "crt_box")
    assert box.run().returncode == 0
    assert "CANOPY_TOKEN=crt_box" in seen.read_text().splitlines()


# --- a machine of several runners --------------------------------------------
#
# One install for every runner, deferred while ANY runner has a turn in flight, every
# `canopy-runner@*` restarted (and the KMS broker), and each runner's credentials
# refreshed AS that runner.

class RunnersBox(Box):
    RUNNERS = ("dimagi", "agent-eva")

    def __init__(self, tmp_path: pathlib.Path):
        super().__init__(tmp_path)
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        runners = []
        for i, uid in enumerate(self.RUNNERS):
            home = tmp_path / "home" / f"cu-{uid}"
            (home / ".canopy-runner").mkdir(parents=True)
            (home / ".canopy-cloud-runner.json").write_text(json.dumps({"runner_id": RUNNER_ID}))
            (home / ".canopy-runner" / "runner-token.json").write_text(json.dumps(
                {"access_token": f"crt_{uid}", "expires_at": time.time() + 3600}))
            src = home / ".canopy-runner" / "src" / "runner" / "ec2"
            src.mkdir(parents=True)
            (src / "bootstrap_agents.sh").write_text(
                f'#!/bin/bash\necho "$CANOPY_TOKEN $1" > "{home}/refreshed-with"\n')
            (src / "bootstrap_agents.sh").chmod(0o755)
            kind, agent = ("agent", "eva") if uid.startswith("agent-") else ("tree", "")
            (self.etc / f"{uid}.env").write_text(
                f"CANOPY_RUNNER_INSTANCE={uid}\nCANOPY_SERVES_KIND={kind}\nCANOPY_SERVES_AGENT={agent}\nHOME={home}\n")
            runners.append({"id": uid, "user": f"cu-{uid}", "home": str(home), "uid": 2001 + i})
        (self.etc / "runners.json").write_text(json.dumps({"box": "cloud-ec2-9", "runners": runners}))
        (self.etc / "box.env").write_text(
            "CANOPY_BASE_URL=https://labs.example/canopy\nRUNNERS=tree:dimagi,agent:eva\n")
        self.systemctl_log = tmp_path / "systemctl.log"
        self._write_stub("fake-systemctl", f'echo "$*" >> "{self.systemctl_log}"\n')
        self.as_runner_log = tmp_path / "as-runner.log"
        # Records its argv, then runs the command as runuser would (minus the user).
        self._write_stub("fake-as-runner", f'echo "$*" | tr "\\n" " " >> "{self.as_runner_log}"; echo >> "{self.as_runner_log}"\nshift 2\nexec "$@"\n')

    def home_of(self, uid: str) -> pathlib.Path:
        return self.root / "home" / f"cu-{uid}"

    def busy_runner(self, uid: str, count: int, age_seconds: float = 0.0) -> None:
        (self.home_of(uid) / ".canopy-runner" / "in-flight").write_text(
            json.dumps({"count": count, "at": time.time() - age_seconds}))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True,
            env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
                 "RUNNER_HOME": str(self.home), "CANOPY_WEB_REPO_DIR": str(self.repo),
                 "RESTART_CMD": str(self.bin / "fake-restart"), "RUNNERS_FILE": str(self.etc / "runners.json"),
                 "BOX_ENV_FILE": str(self.etc / "box.env"), "RUNNER_ETC": str(self.etc),
                 "AS_RUNNER": str(self.bin / "fake-as-runner"),
                 "SYSTEMCTL": str(self.bin / "fake-systemctl"),
                 **self.env()})


@pytest.fixture
def runners_box(tmp_path):
    return RunnersBox(tmp_path)


def test_runners_one_install_restarts_every_runner_and_the_broker(runners_box):
    runners_box.stamp("0" * 40)
    sha = runners_box.seed_repo("#!/usr/bin/env python3\nprint('shipped to every runner')\n")
    runners_box.expect(sha)
    out = runners_box.run()
    assert out.returncode == 0, out.stderr
    assert "shipped to every runner" in runners_box.installed
    restarted = runners_box.restart_log.read_text()
    assert "canopy-runner@*.service" in restarted and "canopy-kms-broker.service" in restarted
    # Root's setup first, so what the new code sets up is in place before its runners start.
    assert runners_box.systemctl_log.read_text().splitlines() == ["restart canopy-runners.service"]
    # It read the fleet list with a RUNNER's own token, never a PAT.
    assert "Bearer crt_dimagi" in runners_box.curl_log.read_text()


def test_runners_any_runner_in_flight_defers_the_install(runners_box):
    runners_box.stamp("0" * 40)
    runners_box.expect(runners_box.seed_repo(GOOD_RUNNER))
    runners_box.busy_runner("dimagi", 0)
    runners_box.busy_runner("agent-eva", 1)
    assert runners_box.run("--check").stdout.startswith("busy")
    assert runners_box.run().returncode == 0
    assert not runners_box.restarted
    # A runner whose marker went stale is not busy (it is the one to rescue).
    runners_box.busy_runner("agent-eva", 3, age_seconds=600)
    assert runners_box.run("--check").stdout.startswith("stale")


def test_runners_each_refreshes_its_credentials_as_itself(runners_box):
    sha = runners_box.seed_repo(GOOD_RUNNER)
    runners_box.stamp(sha)
    runners_box.expect(sha)
    assert runners_box.run().returncode == 0
    lines = runners_box.as_runner_log.read_text().splitlines()
    assert len(lines) == 2
    for uid, line in zip(runners_box.RUNNERS, lines):
        assert line.startswith(f"cu-{uid} -- env -i CANOPY_RUNNER_INSTANCE={uid}")
        # The token never rides the command line (visible in /proc/<pid>/cmdline)…
        assert "crt_" not in line
        # …the runner's own process read it from its own file.
        assert (runners_box.home_of(uid) / "refreshed-with").read_text().strip() == \
            f"crt_{uid} --credentials-only"


# --- needrestart ------------------------------------------------------------
# Ubuntu's unattended-upgrades runs needrestart, which restarts every service
# using an upgraded library. It restarted canopy-runner.service 25 minutes into a
# turn on cloud-ec2-2 (failure investigation 7) and killed it. This updater
# refuses to restart a busy runner; needrestart does not check, so it is told to
# skip every canopy unit. Boxes already up never re-run cloud-init, so the
# updater is what lays the rule down — on every tick, legacy box and several-runner box alike.
NEEDRESTART_RULE = "$nrconf{override_rc}->{qr(^canopy-)} = 0;"


def test_every_tick_tells_needrestart_to_skip_every_canopy_unit(box):
    sha = box.seed_repo(GOOD_RUNNER)
    box.stamp(sha)
    box.expect(sha)
    box.busy(count=1)  # mid-turn is exactly when it matters
    assert box.run().returncode == 0
    assert NEEDRESTART_RULE in box.needrestart_conf.read_text()


def test_a_box_of_runners_gets_the_same_rule(runners_box):
    assert runners_box.run().returncode == 0
    assert NEEDRESTART_RULE in runners_box.needrestart_conf.read_text()


def test_the_rule_matches_the_runner_the_runners_and_the_broker():
    pattern = re.search(r"qr\((.*?)\)", NEEDRESTART_RULE).group(1)
    for unit in ("canopy-runner.service", "canopy-runner@dimagi.service",
                 "canopy-runners.service", "canopy-kms-broker.service", "canopy-pair.service"):
        assert re.search(pattern, unit), unit
    assert not re.search(pattern, "irqbalance.service")


def test_an_older_narrower_rule_is_replaced(box):
    # cloud-ec2-2 got `qr(^canopy-runner)` by hand on 2026-10-16: it misses the
    # broker, whose restart takes every runner's KMS calls down with it.
    box.needrestart_conf.write_text("$nrconf{override_rc}->{qr(^canopy-runner)} = 0;\n")
    box.run()
    assert NEEDRESTART_RULE in box.needrestart_conf.read_text()


def test_the_rule_is_written_once(box):
    box.run()
    box.needrestart_conf.write_text(NEEDRESTART_RULE + "\n# untouched\n")
    box.run()
    assert "# untouched" in box.needrestart_conf.read_text()


def test_check_mode_writes_nothing(box):
    box.run("--check")
    assert not box.needrestart_conf.exists()


def test_a_box_without_needrestart_is_left_alone(box):
    box.needrestart_conf.parent.rmdir()
    assert box.run().returncode == 0
    assert not box.needrestart_conf.exists()


def test_a_failed_write_never_fails_the_tick(box):
    box.needrestart_conf.parent.chmod(0o500)
    try:
        out = box.run()
    finally:
        box.needrestart_conf.parent.chmod(0o755)
    assert out.returncode == 0
    assert "could not write" in out.stdout + out.stderr


def test_new_boxes_get_the_same_rule_from_the_template():
    text = (SCRIPT.parent / "runner.cfn.yaml").read_text()
    assert "path: /etc/needrestart/conf.d/50-canopy-runner.conf" in text
    assert NEEDRESTART_RULE in text
    assert NEEDRESTART_RULE in SCRIPT.read_text()


# --- what no runner cleans --------------------------------------------------
# Each runner sweeps only its own files while its `disk` check measures the whole
# volume. On cloud-ec2-2 (2026-10-26) it warned at 89% while every sweep freed
# 0 MB: 35 GB in the pre-several-runners user's home, 12 GB of /tmp owned by users no runner
# may delete. Root's tick is the one place that sees all of it.
TMP_RULE = "D /tmp 1777 root root 3d"


def test_every_tick_ages_tmp_out_whoever_owns_it(box):
    assert box.run().returncode == 0
    assert TMP_RULE in box.tmpfiles_conf.read_text().splitlines()
    # Idempotent: a second tick leaves the file as it is.
    before = box.tmpfiles_conf.stat().st_mtime_ns
    assert box.run().returncode == 0
    assert box.tmpfiles_conf.stat().st_mtime_ns == before


def test_check_mode_lays_no_tmp_rule(box):
    assert box.run("--check").returncode == 0
    assert not box.tmpfiles_conf.exists()


def _legacy_caches(home: pathlib.Path) -> list[pathlib.Path]:
    caches = [home / ".claude/plugins/cache/ace/ace/0.1", home / ".cache/uv/x",
              home / ".cache/ms-playwright/chromium", home / ".npm/_cacache/y"]
    for c in caches:
        c.mkdir(parents=True)
        (c / "blob").write_text("x" * 100)
    return caches


def _legacy_work(home: pathlib.Path) -> list[pathlib.Path]:
    keep = [home / "work/gwdata/items.jsonl", home / ".ssh/authorized_keys",
            home / ".claude/settings.json", home / ".claude/plugins/installed_plugins.json"]
    for k in keep:
        k.parent.mkdir(parents=True, exist_ok=True)
        k.write_text("keep")
    return keep


def test_a_box_of_runners_drops_the_pre_runners_users_caches_and_keeps_its_work(runners_box):
    caches, work = _legacy_caches(runners_box.legacy_home), _legacy_work(runners_box.legacy_home)
    out = runners_box.run()
    assert out.returncode == 0, out.stderr
    for c in caches:
        assert not c.exists(), c
    for k in work:
        assert k.read_text() == "keep", k
    assert "removed" in out.stdout + out.stderr


def test_the_caches_stay_while_that_user_runs_something(runners_box):
    caches = _legacy_caches(runners_box.legacy_home)
    runners_box.legacy_busy.write_text("")
    assert runners_box.run().returncode == 0
    assert all(c.exists() for c in caches)


def test_a_box_without_runners_keeps_its_own_users_caches(box):
    # There the "legacy" user IS the runner.
    caches = _legacy_caches(box.legacy_home)
    assert box.run().returncode == 0
    assert all(c.exists() for c in caches)


def test_at_warn_root_records_where_the_space_is_for_the_runners(runners_box):
    runners_box.disk_pct.write_text("89")
    (runners_box.root / "home" / "ubuntu").mkdir(parents=True, exist_ok=True)
    (runners_box.root / "tmp").mkdir(exist_ok=True)
    runners_box._write_stub("du", 'for p in "$@"; do case "$p" in -*) ;; '
                                '*ubuntu) printf "37580963840\\t%s\\n" "$p";; '
                                '*) printf "1024\\t%s\\n" "$p";; esac; done\n')
    assert runners_box.run().returncode == 0
    doc = json.loads((runners_box.home / "disk-usage.json").read_text())
    assert doc["pct"] == 89
    assert doc["largest"][0] == [str(runners_box.root / "home" / "ubuntu"), 37580963840]
    assert str(runners_box.root / "tmp") in [p for p, _ in doc["largest"]]
    # Below warn the record goes away, so a runner never quotes a stale one.
    runners_box.disk_pct.write_text("40")
    assert runners_box.run().returncode == 0
    assert not (runners_box.home / "disk-usage.json").exists()
