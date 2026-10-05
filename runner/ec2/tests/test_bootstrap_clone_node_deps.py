"""An agent's clone gets its own node deps at bootstrap.

cloud-ec2-2 came up on 2026-10-05 with ace's readiness drill FAILING: bin/ace-doctor
was BROKEN on a missing node_modules/tsx in /opt/agents/ace. ace-setup had run and
reported OK, but it installs deps into the plugin root it resolves, which on a
fresh box is the plugin cache, not the clone turns run in. cloud-ec2-1 only passed
because something had installed them there by hand.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _extract(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run(clone: pathlib.Path, tmp: pathlib.Path, npm_exit: int = 0) -> str:
    """Run ensure_clone_node_deps against a fake npm that records its argv and cwd."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    npm = bindir / "npm"
    npm.write_text(f'#!/bin/sh\necho "$PWD $*" >> "{tmp}/npm.log"\n'
                   f'[ {npm_exit} -eq 0 ] && mkdir -p node_modules\nexit {npm_exit}\n')
    npm.chmod(0o755)
    # macOS has no coreutils `timeout`; the box and CI do. Pass straight through.
    timeout = bindir / "timeout"
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    timeout.chmod(0o755)
    script =('ok() { echo "OK: $*"; }; warn() { echo "WARN: $*"; }\n'
              f"{_extract('ensure_clone_node_deps')}\nensure_clone_node_deps ace {clone}\n")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _npm_calls(tmp: pathlib.Path) -> list[str]:
    log = tmp / "npm.log"
    return log.read_text().splitlines() if log.exists() else []


def test_a_clone_with_a_lockfile_gets_npm_ci_in_the_clone(tmp_path):
    clone = tmp_path / "ace"
    clone.mkdir()
    (clone / "package.json").write_text("{}")
    (clone / "package-lock.json").write_text("{}")
    out = _run(clone, tmp_path)
    assert _npm_calls(tmp_path) == [f"{clone} ci --silent"]
    assert "OK: ace: npm ci in the clone" in out
    assert (clone / "node_modules").is_dir()


def test_a_clone_without_a_lockfile_gets_npm_install(tmp_path):
    clone = tmp_path / "ace"
    clone.mkdir()
    (clone / "package.json").write_text("{}")
    _run(clone, tmp_path)
    assert _npm_calls(tmp_path) == [f"{clone} install --silent"]


def test_existing_deps_are_left_alone(tmp_path):
    clone = tmp_path / "ace"
    (clone / "node_modules").mkdir(parents=True)
    (clone / "package.json").write_text("{}")
    _run(clone, tmp_path)
    assert _npm_calls(tmp_path) == []


def test_a_clone_with_no_package_json_is_not_touched(tmp_path):
    clone = tmp_path / "ada"
    clone.mkdir()
    _run(clone, tmp_path)
    assert _npm_calls(tmp_path) == []


def test_a_failed_install_warns_and_does_not_fail_bootstrap(tmp_path):
    clone = tmp_path / "ace"
    clone.mkdir()
    (clone / "package.json").write_text("{}")
    out = _run(clone, tmp_path, npm_exit=1)
    assert "WARN: ace: npm install" in out and "failed" in out


def test_the_provisioner_runs_it_even_for_an_agent_with_no_setup_script():
    body = _extract("run_agent_provisioner")
    assert body.index("ensure_clone_node_deps") < body.index("|| return 0")
