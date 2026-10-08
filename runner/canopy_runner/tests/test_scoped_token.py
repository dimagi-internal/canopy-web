"""A colleague's full-profile turn reads canopy as them: the laptop runner leaves their
caller token under the session's emdash task — and clears it for the next turn that has none."""
from __future__ import annotations

import stat

import pytest

from canopy_runner import execute, scoped_token


def test_the_token_is_written_privately_under_the_task(tmp_path):
    p = scoped_token.write({"id": "t1", "mcp_token": "cct_abc"}, task="ace-x1", root=tmp_path)
    assert p == tmp_path / "task" / "ace-x1.token"
    assert p.read_text() == "cct_abc"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_a_reused_task_never_keeps_the_last_askers_token(tmp_path):
    scoped_token.write({"id": "t1", "mcp_token": "cct_abc"}, task="ace-x1", root=tmp_path)
    assert scoped_token.write({"id": "t2"}, task="ace-x1", root=tmp_path) is None
    assert not (tmp_path / "task" / "ace-x1.token").exists()


def test_a_hostile_task_name_is_refused_when_there_is_a_token(tmp_path):
    with pytest.raises(scoped_token.ScopedTokenError):
        scoped_token.write({"id": "t1", "mcp_token": "cct_abc"}, task="../../etc/x", root=tmp_path)
    assert not any(tmp_path.rglob("*.token"))


def test_an_unwritable_root_fails_rather_than_falls_back(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(scoped_token.ScopedTokenError):
        scoped_token.write({"id": "t1", "mcp_token": "cct_abc"}, task="ace-x1", root=blocker)


def test_tokens_older_than_their_lifetime_are_pruned(tmp_path):
    import os

    scoped_token.write({"mcp_token": "cct_a"}, task="old", root=tmp_path, now=lambda: 1_000.0)
    os.utime(tmp_path / "task" / "old.token", (1_000.0, 1_000.0))
    scoped_token.write({"mcp_token": "cct_b"}, task="new", root=tmp_path,
                       now=lambda: 1_000.0 + scoped_token.KEEP_SECONDS + 1)
    assert not (tmp_path / "task" / "old.token").exists()
    assert (tmp_path / "task" / "new.token").exists()


class _Client:
    def __init__(self):
        self.failed = []

    def fail_turn(self, turn_id, note):
        self.failed.append((turn_id, note))


def test_confine_leaves_a_full_turns_token_before_anything_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(scoped_token, "ROOT", tmp_path)
    client = _Client()
    assert execute._confine(client, {"id": "t1", "mcp_token": "cct_abc"}, "ace-x1") is True
    assert (tmp_path / "task" / "ace-x1.token").read_text() == "cct_abc"
    assert client.failed == []


def test_confine_fails_the_turn_when_the_token_cannot_be_left(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(scoped_token, "ROOT", blocker)
    client = _Client()
    assert execute._confine(client, {"id": "t1", "mcp_token": "cct_abc"}, "ace-x1") is False
    assert client.failed and "not run" in client.failed[0][1]
