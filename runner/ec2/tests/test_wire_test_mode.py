"""`wire.sh --test`: stage a test box's credentials and stop, touching no routing.

A test box (`up.sh --test`) is directed-only: it serves nothing until a chat is
pointed at it. Wiring it is therefore discovery, credential staging and the wait
for a staged credential to answer — no predecessor retired, no assignment row,
rule or workspace order edited, so it needs no --agents — and then it says how to
point a chat at the box.
"""
from __future__ import annotations

import pathlib
import subprocess

from test_wire_runner_vault import LIVE, NEW, _row, _setup
from test_wire_probe_wait import _checks

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"


def _wire(tmp, *args):
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t\n")
    return subprocess.run(["bash", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parent),
                          env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home),
                               "WIRE_POLL_SECONDS": "0"})


def _calls(state):
    return (state / "calls").read_text().splitlines()


def test_wire_test_touches_no_routing(tmp_path):
    new = {**_row(NEW, "cloud-ec2-fork", None, health_checks=_checks(**{"subscription-1": "ok"})),
           "workspace": "canopy",
           "capabilities": {"sessions": True, "directed": True, "projects": ["canopy-web"]}}
    # A same-name predecessor that a normal wire would retire: --test must not.
    old = _row(LIVE, "cloud-ec2-fork")
    state = _setup(tmp_path, [new, old])
    res = _wire(tmp_path, "--runner-id", NEW, "--name", "cloud-ec2-fork", "--test",
                "--probe-minutes", "1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "a Claude credential answered" in res.stdout
    calls = _calls(state)
    assert not [c for c in calls if c.startswith("PUT ")], calls
    assert not [c for c in calls if c.endswith("/retire")], calls
    assert not [c for c in calls if "/api/agents/" in c or "runner-order" in c], calls
    assert any(c == f"POST /api/harness/runners/{NEW}/vault" for c in calls)
    assert "/w/canopy/chat" in res.stdout and "cloud-ec2-fork" in res.stdout


def test_wire_test_and_standby_do_not_mix(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-fork", None)])
    res = _wire(tmp_path, "--runner-id", NEW, "--name", "cloud-ec2-fork", "--test",
                "--standby", "--agents", "echo")
    assert res.returncode != 0
    assert not (state / "vaulted.json").exists()


def test_wire_test_waits_for_a_working_credential(tmp_path):
    """A test box whose staged credential does not answer tests nothing: --test
    runs the probe wait too, and says so instead of printing a chat link."""
    new = {**_row(NEW, "cloud-ec2-fork", None, health_checks=_checks(**{"subscription-1": "fail"})),
           "workspace": "canopy", "capabilities": {"sessions": True, "directed": True}}
    state = _setup(tmp_path, [new, _row(LIVE, "cloud-ec2-2")])
    res = _wire(tmp_path, "--runner-id", NEW, "--name", "cloud-ec2-fork", "--test",
                "--probe-minutes", "1")
    assert res.returncode == 3
    assert "+ Add a login" in res.stderr
    assert "/w/canopy/chat" not in res.stdout
    assert not [c for c in _calls(state) if c.startswith("PUT ")]
