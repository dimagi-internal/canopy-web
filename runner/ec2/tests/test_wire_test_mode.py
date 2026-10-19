"""`wire.sh --test`: stage a test box's credentials and stop, touching no routing.

A test box (`up.sh --test`) is directed-only: it serves nothing until a chat is
pointed at it. Wiring it is therefore discovery and credential staging only — no
predecessor retired, no assignment row, rule or workspace order edited, so it
needs no --agents — and then it says how to point a chat at the box.
"""
from __future__ import annotations

import pathlib
import subprocess

from test_wire_credential_source import LIVE, NEW, _row, _setup

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"


def _wire(tmp, *args):
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t\n")
    return subprocess.run(["bash", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parent),
                          env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home)})


def _calls(state):
    return (state / "calls").read_text().splitlines()


def test_wire_test_touches_no_routing(tmp_path):
    new = {**_row(NEW, "cloud-ec2-fork", None), "workspace": "canopy",
           "capabilities": {"sessions": True, "directed": True, "projects": ["canopy-web"]}}
    # A same-name predecessor that a normal wire would retire: --test must not.
    old = _row(LIVE, "cloud-ec2-fork")
    state = _setup(tmp_path, [new, old])
    res = _wire(tmp_path, "--runner-id", NEW, "--name", "cloud-ec2-fork", "--test")
    assert res.returncode == 0, res.stdout + res.stderr
    calls = _calls(state)
    assert not [c for c in calls if c.startswith("PUT ")], calls
    assert not [c for c in calls if c.endswith("/retire")], calls
    assert not [c for c in calls if "/api/agents/" in c or "runner-order" in c], calls
    assert any(c == f"POST /api/harness/runners/{NEW}/credential" for c in calls)
    assert "/w/canopy/chat" in res.stdout and "cloud-ec2-fork" in res.stdout


def test_wire_test_and_standby_do_not_mix(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-fork", None)])
    res = _wire(tmp_path, "--runner-id", NEW, "--name", "cloud-ec2-fork", "--test",
                "--standby", "--agents", "echo")
    assert res.returncode != 0
    assert not (state / "posted.json").exists()
