"""wire.sh routes nothing to a box until one of its credentials has answered a
prompt (the box's `claude.probe.<label>` health checks) — signed in by its owner on
the runner's Credentials tab while wire.sh waits.

cloud-ec2-fork, 2026-10-18: wired onto a token at its weekly cap, it failed every
turn as "not signed in". The stored value was there; it just did not work.
"""
from __future__ import annotations

from test_wire_runner_vault import LIVE, NEW, _row, _setup, _wire


AFTER = "2026-10-19T12:01:00Z"


def _checks(probed=AFTER, **statuses):
    return {f"claude.probe.{label}": {"name": f"claude.probe.{label}", "status": st,
                                      "detail": ("at its usage cap — resets 2026-10-19T23:00:00Z"
                                                 if st == "warn" else "x") + f" · probed {probed}"}
            for label, st in statuses.items()}


def test_probe_wait_fails_when_nothing_works(tmp_path):
    state = _setup(tmp_path, [
        _row(NEW, "cloud-ec2-1", None, health_checks=_checks(**{"subscription-1": "warn",
                                                                "subscription-2": "fail"})),
        _row(LIVE, "cloud-ec2-1"),
    ])
    res = _wire(tmp_path, probe_minutes="1")
    assert res.returncode != 0
    assert "+ Add a login" in res.stderr
    assert f"/w/dimagi/runners/{NEW}/credentials" in res.stderr
    assert "subscription-1" in res.stdout and "resets" in res.stdout
    calls = (state / "calls").read_text()
    assert "PUT /api/agents/" not in calls
    assert "/retire" not in calls


def test_probe_wait_passes_on_one_ok(tmp_path):
    _setup(tmp_path, [
        _row(NEW, "cloud-ec2-1", None, health_checks=_checks(**{"subscription-1": "warn",
                                                                "api-key": "ok"})),
        _row(LIVE, "cloud-ec2-1"),
    ])
    res = _wire(tmp_path, probe_minutes="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "a Claude credential answered" in res.stdout


def test_a_box_that_reports_no_probe_is_not_wired(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-1", None), _row(LIVE, "cloud-ec2-1")])
    res = _wire(tmp_path, probe_minutes="1")
    assert res.returncode != 0
    assert "reported no credential" in res.stderr
    assert "/retire" not in (state / "calls").read_text()
