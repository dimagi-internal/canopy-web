"""The runner suite run inside an agent's turn on a runner box must not reach that box's
live runner: its state paths, its relayed turns, its identity (conftest.py)."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

EC2 = pathlib.Path(__file__).resolve().parent.parent


def _live_box(tmp_path: pathlib.Path) -> tuple[dict, pathlib.Path, pathlib.Path]:
    """The environment a turn on a supervised box inherits from its runner, over a
    fake home holding one running turn's relay state and an in-flight marker."""
    home = tmp_path / "cu-dimagi"
    state = home / ".canopy-runner"
    turn = state / "turns" / "bbb19daa-fa4f-490f-9dfa-987e4c67361a"
    (turn / "a1").mkdir(parents=True)
    (turn / "turn.json").write_text('{"runner_id": "live-runner", "turn": {"id": "bbb19daa"}}')
    marker = state / "in-flight"
    marker.write_text('{"count": 1, "pinned": 0, "handoff": 1}')
    env = {
        **os.environ,
        "CANOPY_RUNNER_INSTANCE": "live-runner", "CANOPY_RUNNER_SUPERVISED": "1",
        "RUNNER_EXECUTOR": "acp", "HOME": str(home),
        "STATE_FILE": str(home / ".canopy-cloud-runner.json"),
        "IN_FLIGHT_FILE": str(marker),
        "RUNNER_TOKEN_FILE": str(state / "runner-token.json"),
        "RUNNER_SRC_DIR": str(state / "src"),
    }
    return env, turn, marker


def test_tests_that_drive_turns_and_adoption_leave_a_live_runner_alone(tmp_path):
    env, turn, marker = _live_box(tmp_path)
    before = marker.read_text()
    # Files that, with a live runner's environment, deleted a running turn's relay
    # state (connection staging, lazy imports), rewrote its in-flight marker (health,
    # turn starts) or wrote its runner token (cloud_runner).
    targets = [
        "tests/test_connection_token_staging.py",
        "tests/test_lazy_package_imports.py",
        "tests/test_runner_health.py",
        "tests/test_cloud_runner.py",
    ]
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                           *targets], cwd=EC2, env=env, capture_output=True, text=True, timeout=300)
    assert turn.is_dir() and (turn / "turn.json").exists(), "a live turn's relay state was deleted"
    assert marker.read_text() == before, "the live runner's in-flight marker was rewritten"
    assert not (tmp_path / "cu-dimagi" / ".canopy-runner" / "runner-token.json").exists(), \
        "the live runner's token file was written"
    assert not (tmp_path / "cu-dimagi" / ".canopy-bootstrapped-at").exists()
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
