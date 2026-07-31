"""The update doorbell, cloud half: an `update_available` WS frame starts the
SEPARATE systemd update unit instead of waiting out its 30-minute timer.

The daemon never installs in-process — and must not even run the script as a
child: the updater restarts canopy-runner.service, and a child would die in the
daemon's own cgroup mid-install. `systemctl start` hands the work to systemd's
cgroup, where the restart it performs cannot kill it. The unit re-checks
staleness and the in-flight marker itself, so busy deferral and the crash-loop
rescue are inherited, not re-implemented.
"""
from __future__ import annotations

import pytest

SHIPPED = "a" * 40
OLD = "b" * 40


@pytest.fixture
def runner(tmp_path, monkeypatch, load_cloud_runner):
    monkeypatch.setenv("RUNNER_HOME", str(tmp_path))
    monkeypatch.setenv("BUILD_INFO_FILE", str(tmp_path / "build-info.json"))
    monkeypatch.setenv("IN_FLIGHT_FILE", str(tmp_path / "in-flight"))
    mod = load_cloud_runner()
    mod._last_update_nudge = 0.0
    return mod


@pytest.fixture
def kicks(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_start_update_unit", lambda: calls.append(1))
    monkeypatch.setattr(runner, "build_info",
                        lambda refresh=False: {"sha": OLD, "committed_at": 1})
    return calls


def test_nudge_starts_the_update_unit(runner, kicks):
    assert runner._nudge_updater(SHIPPED) is True
    assert len(kicks) == 1


def test_nudge_skips_when_already_current(runner, kicks, monkeypatch):
    monkeypatch.setattr(runner, "build_info",
                        lambda refresh=False: {"sha": SHIPPED, "committed_at": 1})
    assert runner._nudge_updater(SHIPPED) is False
    assert kicks == []


def test_nudge_throttles_repeat_frames(runner, kicks):
    # The server rings on EVERY stale heartbeat (~20s). One kick per window —
    # the unit may be deliberately deferring (busy) for hours.
    assert runner._nudge_updater(SHIPPED) is True
    assert runner._nudge_updater(SHIPPED) is False
    assert len(kicks) == 1


def test_nudge_fires_again_after_the_window(runner, kicks):
    import time
    assert runner._nudge_updater(SHIPPED) is True
    later = time.time() + runner.UPDATE_NUDGE_MIN_SECONDS + 1
    assert runner._nudge_updater(SHIPPED, now=later) is True
    assert len(kicks) == 2


def test_empty_expected_never_kicks(runner, kicks):
    # Empty means UNKNOWN, never "stale" — the fleet-wide provenance rule.
    assert runner._nudge_updater("") is False
    assert kicks == []


def test_unknown_installed_sha_never_kicks(runner, kicks, monkeypatch):
    monkeypatch.setattr(runner, "build_info",
                        lambda refresh=False: {"sha": "", "committed_at": 0})
    assert runner._nudge_updater(SHIPPED) is False
    assert kicks == []


def test_a_failing_start_never_raises(runner, monkeypatch):
    # This runs on the WS loop that also carries wake and heartbeat — a
    # systemctl hiccup must not cost the socket.
    def boom():
        raise OSError("systemctl went away")
    monkeypatch.setattr(runner, "_start_update_unit", boom)
    monkeypatch.setattr(runner, "build_info",
                        lambda refresh=False: {"sha": OLD, "committed_at": 1})
    assert runner._nudge_updater(SHIPPED) is False
