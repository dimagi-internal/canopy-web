"""Auto-update: should this box install a newer runner right now?

Spec: docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md
"""
import json

import pytest

from canopy_runner import update
from canopy_runner.config import Config

SHIPPED = "a" * 40
OLD = "b" * 40


@pytest.fixture()
def cfg(tmp_path):
    return Config(base_url="http://x", token="t", runner_id="r-1",
                  emdash_db=str(tmp_path / "e.db"),
                  state_path=str(tmp_path / "state.json"))


class FakeClient:
    def __init__(self, rows=None, boom=None):
        self._rows = rows if rows is not None else []
        self._boom = boom
        self.calls = []

    def list_runners(self):
        self.calls.append("list_runners")
        if self._boom:
            raise self._boom
        return self._rows

    def heartbeat(self, *a, **k):
        pytest.fail("the updater must NEVER heartbeat — it would forge liveness "
                    "for a daemon that may be dead, and clobber its provenance")


def _row(**kw):
    base = {"id": "r-1", "name": "mbp", "expected_code_sha": SHIPPED}
    base.update(kw)
    return base


# --- the comparison ---------------------------------------------------------


def test_current_when_installed_matches_what_shipped(cfg):
    status, expected = update.update_status(cfg, FakeClient([_row()]), installed_sha=SHIPPED)
    assert status == update.CURRENT and expected == SHIPPED


def test_stale_when_they_differ_and_nothing_is_in_flight(cfg):
    status, expected = update.update_status(cfg, FakeClient([_row()]), installed_sha=OLD)
    assert status == update.STALE and expected == SHIPPED


def test_unknown_when_the_server_has_no_expectation(cfg):
    """A dev server bakes in no RUNNER_CODE_SHA. Empty means UNKNOWN, never
    "different" — auto-installing an empty sha would be a reinstall loop against
    a target that does not exist."""
    status, _ = update.update_status(cfg, FakeClient([_row(expected_code_sha="")]),
                                     installed_sha=OLD)
    assert status == update.UNKNOWN


def test_unknown_when_this_box_cannot_say_what_it_is_running(cfg):
    status, _ = update.update_status(cfg, FakeClient([_row()]), installed_sha="")
    assert status == update.UNKNOWN


def test_unknown_when_the_runner_is_not_in_the_fleet_list(cfg):
    """Retired, or invisible to this token. Reinstalling would not fix either."""
    status, _ = update.update_status(cfg, FakeClient([_row(id="someone-else")]),
                                     installed_sha=OLD)
    assert status == update.UNKNOWN


def test_a_flaky_network_never_triggers_an_update(cfg):
    """The timer fires every 30 min; a blip must not be read as "you're behind"."""
    status, _ = update.update_status(cfg, FakeClient(boom=RuntimeError("no route")),
                                     installed_sha=OLD)
    assert status == update.UNKNOWN


# --- the idle gate ----------------------------------------------------------


def test_busy_defers_the_update(cfg):
    """An update restarts the daemon, and a chat turn is bridged across ticks —
    restarting mid-turn strands the reply."""
    update.mark_busy(cfg, 2)
    status, _ = update.update_status(cfg, FakeClient([_row()]), installed_sha=OLD)
    assert status == update.BUSY


def test_idle_marker_allows_the_update(cfg):
    update.mark_busy(cfg, 0)
    status, _ = update.update_status(cfg, FakeClient([_row()]), installed_sha=OLD)
    assert status == update.STALE


def test_a_stale_marker_does_not_block_forever(cfg):
    """The loop rewrites the marker every tick, so an OLD marker means the daemon
    is not looping — stopped, wedged or crash-looping. That is precisely the case
    auto-update exists to rescue, so it must not read as "busy" and block the fix."""
    update.mark_busy(cfg, 5)
    later = update.BUSY_MARKER_MAX_AGE + 60
    import time as _t
    status, _ = update.update_status(cfg, FakeClient([_row()]), installed_sha=OLD,
                                     now=_t.time() + later)
    assert status == update.STALE


def test_a_missing_marker_does_not_block(cfg):
    """A runner that has never run has no marker; so does one whose disk write
    failed. Neither is evidence of work in flight."""
    assert update.in_flight(cfg) is None
    status, _ = update.update_status(cfg, FakeClient([_row()]), installed_sha=OLD)
    assert status == update.STALE


def test_a_corrupt_marker_is_treated_as_unknown_not_busy(cfg, tmp_path):
    (tmp_path / "in-flight").write_text("{not json")
    assert update.in_flight(cfg) is None


def test_mark_busy_survives_an_unwritable_state_dir(tmp_path):
    bad = Config(base_url="http://x", token="t", runner_id="r-1", emdash_db="x",
                 state_path="/proc/nope/state.json")
    update.mark_busy(bad, 1)  # must not raise — a marker failure can't break a turn


def test_the_marker_round_trips(cfg):
    update.mark_busy(cfg, 3)
    assert update.in_flight(cfg) == 3
    raw = json.loads((__import__("pathlib").Path(cfg.state_path).parent / "in-flight").read_text())
    assert raw["count"] == 3 and raw["at"] > 0
