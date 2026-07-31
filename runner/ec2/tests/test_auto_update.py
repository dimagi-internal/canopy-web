"""The runner's half of auto-update: what it says it is, and when it is busy.

Two files it shares with runner/ec2/update_runner.sh, and one invariant about how
they are reported. See
docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md.
"""
from __future__ import annotations

import json
import pathlib
import re
import time

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "cloud_runner.py"


@pytest.fixture
def runner(tmp_path, monkeypatch, load_cloud_runner):
    """A module whose RUNNER_HOME is a temp dir — set BEFORE import, because the
    two paths are resolved at module level."""
    monkeypatch.setenv("RUNNER_HOME", str(tmp_path))
    monkeypatch.setenv("BUILD_INFO_FILE", str(tmp_path / "build-info.json"))
    monkeypatch.setenv("IN_FLIGHT_FILE", str(tmp_path / "in-flight"))
    return load_cloud_runner()


def _stamp(tmp_path, **fields):
    (tmp_path / "build-info.json").write_text(json.dumps(fields))


# --- what code am I running -------------------------------------------------
def test_build_info_reads_the_stamp(runner, tmp_path):
    _stamp(tmp_path, sha="abc123", committed_at=1753900000)
    info = runner.build_info(refresh=True)
    assert info == {"sha": "abc123", "committed_at": 1753900000}


def test_a_missing_stamp_is_unknown_not_an_error(runner):
    # /opt/canopy-runner has no git history, so an unstamped install genuinely
    # cannot know. Unknown must be silent, never a crash on the heartbeat path.
    assert runner.build_info(refresh=True) == {"sha": "", "committed_at": 0}


@pytest.mark.parametrize("body", ["not json at all", "[]", '{"sha": null}', ""])
def test_a_malformed_stamp_is_unknown(runner, tmp_path, body):
    (tmp_path / "build-info.json").write_text(body)
    assert runner.build_info(refresh=True)["sha"] == ""


def test_a_non_numeric_committed_at_does_not_break_the_sha(runner, tmp_path):
    _stamp(tmp_path, sha="abc123", committed_at="nonsense")
    # Both fields come from one parse, so a bad timestamp currently costs the sha
    # too — assert whichever way it degrades, it degrades to UNKNOWN and not to a
    # raised exception on the heartbeat path.
    info = runner.build_info(refresh=True)
    assert info["committed_at"] == 0


def test_build_info_is_cached_for_the_process_lifetime(runner, tmp_path):
    # The updater rewrites the stamp moments BEFORE restarting the service. If the
    # running process re-read it live, it would report the new sha while still
    # executing the old bytes — clearing the staleness banner for a box that is
    # still stale. Same reasoning as provenance.code_sha's functools.cache.
    _stamp(tmp_path, sha="old", committed_at=1)
    assert runner.build_info(refresh=True)["sha"] == "old"
    _stamp(tmp_path, sha="new", committed_at=2)
    assert runner.build_info()["sha"] == "old"
    assert runner.build_info(refresh=True)["sha"] == "new"


# --- is now a safe moment ---------------------------------------------------
def test_mark_in_flight_writes_the_shape_the_laptop_writes(runner, tmp_path):
    runner._mark_in_flight(3)
    raw = json.loads((tmp_path / "in-flight").read_text())
    assert raw["count"] == 3
    assert abs(raw["at"] - time.time()) < 5
    # `count` + `at`, and nothing else: update.mark_busy writes exactly this, and
    # runner/ec2/update_runner.sh reads both runners' markers with one parser.
    assert set(raw) == {"count", "at"}


def test_marking_in_flight_never_raises(runner, monkeypatch, tmp_path):
    # Best-effort by contract: a failure here must not touch the turn it is
    # reporting on.
    monkeypatch.setattr(runner, "IN_FLIGHT_FILE", tmp_path / "nope" / "x" / "in-flight")
    monkeypatch.setattr(pathlib.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError))
    runner._mark_in_flight(1)  # must not raise


# --- one stamping point -----------------------------------------------------
def test_heartbeat_body_carries_provenance_and_marks_the_marker(runner, tmp_path):
    _stamp(tmp_path, sha="abc123", committed_at=1753900000)
    runner.build_info(refresh=True)
    body = runner._heartbeat_body(["t1", "t2"])
    assert body["code_sha"] == "abc123"
    assert body["code_committed_at"] == 1753900000
    assert body["active_turn_ids"] == ["t1", "t2"]
    assert body["host"] == runner.RUNNER_HOST
    assert json.loads((tmp_path / "in-flight").read_text())["count"] == 2


def test_heartbeat_body_reports_no_branch(runner):
    # /opt/canopy-runner is not a checkout. Reporting a branch would be inventing
    # one, and the supervisor alerts on any branch that is not `main`.
    assert "code_branch" not in runner._heartbeat_body([])


def test_heartbeat_body_passes_extras_through(runner):
    body = runner._heartbeat_body([], projects=["canopy-web"])
    assert body["projects"] == ["canopy-web"]


def test_every_heartbeat_call_site_goes_through_the_one_stamping_point():
    """The regression guard, and the reason `_heartbeat_body` exists.

    `services.heartbeat` assigns the provenance fields unconditionally, so a
    heartbeat that omits them CLEARS them — a single call site left behind erases
    what the other three report. The laptop paid for this exact bug with
    `code_branch` (four of six sites silently resetting it, see provenance.py);
    the fix there and here is one payload builder.

    Asserted on the SOURCE because the failure is which dict a call site passes,
    which no amount of running the module reveals.
    """
    source = SOURCE.read_text()
    sites = [
        line.strip() for line in source.splitlines()
        if re.search(r'/heartbeat"|"action": "heartbeat"', line)
        and not line.strip().startswith("#")
    ]
    assert len(sites) >= 4, f"expected every heartbeat call site, found {sites}"
    for site in sites:
        # The payload may be on the same line as the URL or the next one; assert on
        # the window rather than the line so formatting cannot defeat the check.
        idx = source.index(site)
        window = source[idx:idx + 400]
        assert "_heartbeat_body" in window, (
            f"heartbeat call site builds its own payload and will erase provenance: {site}"
        )
