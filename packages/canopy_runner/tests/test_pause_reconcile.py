"""The pause is ONE state, settable from either end, with the server as truth.

The local `~/.canopy/PAUSED` sentinel is a CONTROL SURFACE (what the menu-bar app
toggles), not a second copy of the state: a change to it becomes a call to
/pause or /unpause, and otherwise the server's value is written back down. These
tests pin the two ways that could go wrong, both of which silently unpause a box
that must not spend tokens:

  1. LEVEL-REPORT CLOBBER — if the runner asserted "not paused" on every tick
     instead of only on a CHANGE, a remote pause would be lifted by the next
     heartbeat, five seconds after it landed.
  2. MISSING EDGE HISTORY — without the mirror file, "the human just deleted the
     sentinel" (an unpause command) is indistinguishable from "the file was never
     there and the pause came from the server" (obey it). Collapsing those means
     the local file can only ever ADD a pause, which breaks the menu-bar toggle's
     off position — or, worse, that a server pause is fought every tick.

Enforcement does NOT depend on any of this: `claim_next_turn` refuses a paused
runner server-side. This is about the work the runner starts BY ITSELF — inbox
polls and due schedules — which enqueue turns it could not then claim, building a
backlog that stampedes on resume.
"""
from __future__ import annotations

import pytest

from canopy_runner.main import reconcile_pause


class _Cfg:
    runner_id = "r1"

    def __init__(self, tmp_path):
        self.state_path = str(tmp_path / "state.json")


class _Client:
    """Records the pause commands sent, so a test can assert on the EDGE."""

    def __init__(self, fail=False):
        self.calls: list[tuple[bool, str]] = []
        self.fail = fail

    def set_paused(self, runner_id, paused, note=""):
        if self.fail:
            raise RuntimeError("network down")
        self.calls.append((paused, note))
        return {"paused": paused}


@pytest.fixture
def cfg(tmp_path):
    return _Cfg(tmp_path)


def _sentinel(cfg):
    from pathlib import Path
    return Path(cfg.state_path).with_name("PAUSED")


def _mirror(cfg):
    from pathlib import Path
    return Path(cfg.state_path).with_name(".pause-mirror")


# --- the steady state: server is truth --------------------------------------------

def test_unpaused_everywhere_is_a_no_op(cfg):
    client = _Client()
    assert reconcile_pause(cfg, client, server_paused=False) is False
    assert client.calls == []
    assert not _sentinel(cfg).exists()


def test_a_remote_pause_is_obeyed_and_mirrored_down(cfg):
    """No local change, so the server wins — and the sentinel is written so the
    menu-bar app shows the truth instead of a stale toggle."""
    client = _Client()

    assert reconcile_pause(cfg, client, server_paused=True) is True

    assert client.calls == [], "obeying the server must not echo a command back"
    assert _sentinel(cfg).exists()


def test_a_remote_pause_is_NOT_re_asserted_every_tick(cfg):
    """The level-report clobber, from the other side: once mirrored, the sentinel
    now matches, and a naive implementation would read that as a local edge and
    push a redundant command forever."""
    client = _Client()
    reconcile_pause(cfg, client, server_paused=True)

    for _ in range(3):
        assert reconcile_pause(cfg, client, server_paused=True) is True

    assert client.calls == []


def test_a_remote_unpause_removes_the_local_sentinel(cfg):
    client = _Client()
    reconcile_pause(cfg, client, server_paused=True)
    assert _sentinel(cfg).exists()

    assert reconcile_pause(cfg, client, server_paused=False) is False

    assert not _sentinel(cfg).exists()
    assert client.calls == []


# --- the local edge: a change is a command ----------------------------------------

def test_dropping_the_sentinel_pushes_a_pause_up(cfg):
    client = _Client()
    reconcile_pause(cfg, client, server_paused=False)   # settle: agreed, unpaused
    _sentinel(cfg).touch()                              # the human toggles it

    assert reconcile_pause(cfg, client, server_paused=False) is True

    assert len(client.calls) == 1
    assert client.calls[0][0] is True
    assert "PAUSED" in client.calls[0][1]


def test_removing_the_sentinel_pushes_an_unpause_up(cfg):
    """The menu-bar toggle's OFF position. This is the case that needs the mirror:
    the file is absent and the server says paused, which is ALSO what an obeyed
    remote pause looks like."""
    client = _Client()
    _sentinel(cfg).touch()
    reconcile_pause(cfg, client, server_paused=False)   # settle: locally paused
    client.calls.clear()
    _sentinel(cfg).unlink()                             # the human toggles it off

    assert reconcile_pause(cfg, client, server_paused=True) is False

    assert client.calls == [(False, "")]


def test_a_local_edge_beats_a_disagreeing_server(cfg):
    """The edge is the newer intent, so it wins the tick it happens."""
    client = _Client()
    reconcile_pause(cfg, client, server_paused=True)    # settle: paused, mirrored
    client.calls.clear()
    _sentinel(cfg).unlink()                             # human unpauses locally

    assert reconcile_pause(cfg, client, server_paused=True) is False
    assert client.calls == [(False, "")]


def test_the_edge_is_pushed_once_not_every_tick(cfg):
    client = _Client()
    reconcile_pause(cfg, client, server_paused=False)
    _sentinel(cfg).touch()
    reconcile_pause(cfg, client, server_paused=False)
    assert len(client.calls) == 1

    # The server now agrees (it accepted the command), so subsequent ticks are steady.
    for _ in range(3):
        assert reconcile_pause(cfg, client, server_paused=True) is True
    assert len(client.calls) == 1


# --- surviving a restart -----------------------------------------------------------

def test_a_pause_survives_a_runner_restart(cfg):
    """The mirror is on disk precisely so process death is not an edge. A fresh
    process seeing sentinel-present + server-paused must stay quiet, not re-push."""
    client = _Client()
    reconcile_pause(cfg, client, server_paused=True)
    client.calls.clear()

    # A new process: no in-memory state at all, only what is on disk.
    assert reconcile_pause(cfg, client, server_paused=True) is True
    assert client.calls == []


# --- failure modes: fail toward the control plane ----------------------------------

def test_a_failed_push_errs_toward_paused(cfg):
    """The human parked this box; one failed HTTP call must not make it keep
    working. Wrongly pausing costs idleness someone notices — wrongly running
    costs tokens on an account that must not spend them."""
    client = _Client(fail=True)
    reconcile_pause(cfg, client, server_paused=False)
    _sentinel(cfg).touch()

    assert reconcile_pause(cfg, client, server_paused=False) is True


def test_a_failed_push_is_retried_and_not_reverted(cfg):
    """THE subtle one. If a failed push still advanced the mirror, the next tick
    would see local == mirror, fall into the server-wins branch, and DELETE the
    sentinel the human just dropped — undoing a pause because of one flaky call."""
    client = _Client(fail=True)
    reconcile_pause(cfg, client, server_paused=False)
    _sentinel(cfg).touch()
    reconcile_pause(cfg, client, server_paused=False)     # push fails

    assert _sentinel(cfg).exists(), "the pause must survive the failed push"

    client.fail = False
    assert reconcile_pause(cfg, client, server_paused=False) is True
    assert client.calls == [(True, "paused locally (~/.canopy/PAUSED)")]


def test_an_unreadable_state_dir_falls_back_to_the_server(cfg, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "exists", lambda self: (_ for _ in ()).throw(OSError()))
    assert reconcile_pause(cfg, _Client(), server_paused=True) is True
