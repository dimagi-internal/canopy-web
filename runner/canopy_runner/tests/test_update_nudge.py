"""The update doorbell: an `update_available` control frame kickstarts the
SEPARATE updater job instead of waiting out its 30-minute timer.

The daemon never installs anything on this signal — the updater re-checks
staleness and the in-flight marker itself, so every safety property of the
timer path (busy deferral, read-only check, crash-loop rescue) is inherited
rather than re-implemented. The frame is sent on every stale heartbeat, so the
daemon owns the throttle.
"""
import pytest

from canopy_runner import provenance, update
from canopy_runner.config import Config

SHIPPED = "a" * 40
OLD = "b" * 40


@pytest.fixture()
def cfg(tmp_path):
    return Config(base_url="http://x", token="t", runner_id="r-1",
                  emdash_db=str(tmp_path / "e.db"),
                  state_path=str(tmp_path / "state.json"))


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(update, "_last_nudge_at", 0.0)


@pytest.fixture()
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(update, "_kickstart_updater", lambda: calls.append(1))
    monkeypatch.setattr(provenance, "code_sha", lambda: OLD)
    return calls


def test_nudge_kickstarts_the_updater(cfg, kicks):
    assert update.nudge(cfg, SHIPPED) is True
    assert len(kicks) == 1


def test_nudge_skips_when_already_current(cfg, kicks, monkeypatch):
    # The frame raced an install that already happened (or our own heartbeat
    # simply hadn't reported the new sha yet) — nothing to do.
    monkeypatch.setattr(provenance, "code_sha", lambda: SHIPPED)
    assert update.nudge(cfg, SHIPPED) is False
    assert kicks == []


def test_nudge_throttles_repeat_frames(cfg, kicks):
    # The server rings on EVERY stale heartbeat (~10s). The updater needs one
    # kick, not sixty an hour while it is deliberately deferring (busy).
    assert update.nudge(cfg, SHIPPED) is True
    assert update.nudge(cfg, SHIPPED) is False
    assert len(kicks) == 1


def test_nudge_fires_again_after_the_throttle_window(cfg, kicks):
    import time
    assert update.nudge(cfg, SHIPPED) is True
    assert update.nudge(cfg, SHIPPED, now=time.time() + update.NUDGE_MIN_SECONDS + 1) is True
    assert len(kicks) == 2


def test_empty_expected_never_kicks(cfg, kicks):
    # Empty means UNKNOWN, never "stale" — same rule as update_status.
    assert update.nudge(cfg, "") is False
    assert kicks == []


def test_unknown_installed_sha_never_kicks(cfg, kicks, monkeypatch):
    monkeypatch.setattr(provenance, "code_sha", lambda: "")
    assert update.nudge(cfg, SHIPPED) is False
    assert kicks == []


def test_a_failing_kickstart_never_raises(cfg, monkeypatch):
    # This runs on the wake-listener thread, which also carries cancel and
    # wake — losing the socket over a launchctl hiccup is never worth it.
    def boom():
        raise OSError("launchctl went away")
    monkeypatch.setattr(update, "_kickstart_updater", boom)
    monkeypatch.setattr(provenance, "code_sha", lambda: OLD)
    assert update.nudge(cfg, SHIPPED) is False
