"""wire.sh finds the fresh box by what it lacks — a staged credential — not by
"never heartbeated", which stopped being true when the credential wait started
heartbeating. A paired-but-unstaged cloud-ec2-fork was never found (2026-10-18).
"""
from __future__ import annotations

import datetime as dt
import json

from test_wire_credential_source import LIVE, NEW, OTHER, _row, _setup, _wire

def _ago(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


UNSTAGED = {"has_claude_token": False, "has_claude_token_secondary": False,
            "has_claude_api_key": False, "logins": []}


def test_discovers_a_heartbeating_unstaged_box(tmp_path):
    state = _setup(tmp_path, [
        _row(OTHER, "cloud-ec2-fork", _ago(5)),   # staged predecessor, newer beat
        _row(NEW, "cloud-ec2-fork", _ago(20), ready=False),
        _row(LIVE, "cloud-ec2-2"),
    ], statuses={NEW: UNSTAGED})
    res = _wire(tmp_path, "--name", "cloud-ec2-fork", runner_id=None)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"==> runner: {NEW}" in res.stdout
    calls = (state / "calls").read_text()
    assert f"POST /api/harness/runners/{NEW}/vault" in calls
    assert f"POST /api/harness/runners/{OTHER}/credential" not in calls


def test_a_signed_in_login_counts_as_staged(tmp_path):
    _setup(tmp_path, [
        _row(OTHER, "cloud-ec2-fork", _ago(5)),
        _row(NEW, "cloud-ec2-fork", _ago(20)),
    ], statuses={
        OTHER: {**UNSTAGED, "logins": [{"key": "default", "account": "a@x", "signed_in": True}]},
        NEW: UNSTAGED,
    })
    res = _wire(tmp_path, "--name", "cloud-ec2-fork", runner_id=None)
    assert f"==> runner: {NEW}" in res.stdout


def test_a_retired_row_is_never_the_new_box(tmp_path):
    _setup(tmp_path, [
        _row(OTHER, "cloud-ec2-fork", _ago(5), status="retired"),
        _row(NEW, "cloud-ec2-fork", _ago(10)),
    ], statuses={OTHER: UNSTAGED, NEW: UNSTAGED})
    res = _wire(tmp_path, "--name", "cloud-ec2-fork", runner_id=None)
    assert f"==> runner: {NEW}" in res.stdout
    assert json.loads((tmp_path / "state" / "vaulted.json").read_text())


def test_a_dead_unstaged_row_is_not_the_new_box(tmp_path):
    """A box that paired and died is unstaged too; only one heartbeating now
    (online, within 2 minutes) is the one waiting for its bundle."""
    _setup(tmp_path, [
        _row(OTHER, "cloud-ec2-fork", _ago(5), status="offline"),
        _row(LIVE, "cloud-ec2-fork", _ago(600)),
        _row(NEW, "cloud-ec2-fork", _ago(30)),
    ], statuses={OTHER: UNSTAGED, LIVE: UNSTAGED, NEW: UNSTAGED})
    res = _wire(tmp_path, "--name", "cloud-ec2-fork", runner_id=None)
    assert f"==> runner: {NEW}" in res.stdout
