"""The laptop runner leaves a chat's key under the names the chat's own session can
work out — its emdash task and its Claude session id — and nowhere else."""
from __future__ import annotations

import stat

from canopy_runner import chat_key


def test_the_key_is_written_privately_under_the_task_and_the_session(tmp_path):
    turn = {"id": "t1", "chat_key": "chk_abc"}
    paths = chat_key.write(turn, task="hal-canopy-web-chat-x1", transcript_id="eb742bd8-aaaa",
                           root=tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in paths) == [
        "session/eb742bd8-aaaa.key", "task/hal-canopy-web-chat-x1.key"]
    for p in paths:
        assert p.read_text() == "chk_abc"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_no_key_writes_nothing_and_a_hostile_name_is_refused(tmp_path):
    assert chat_key.write({"id": "t1"}, task="x", root=tmp_path) == []
    assert chat_key.write({"id": "t1", "chat_key": "chk_abc"}, task="../../etc/passwd",
                          root=tmp_path) == []
    assert not any(tmp_path.rglob("*.key"))


def test_keys_older_than_their_lifetime_are_pruned(tmp_path):
    turn = {"id": "t1", "chat_key": "chk_abc"}
    chat_key.write(turn, task="old", root=tmp_path, now=lambda: 1_000.0)
    import os
    os.utime(tmp_path / "task" / "old.key", (1_000.0, 1_000.0))
    chat_key.write(turn, task="new", root=tmp_path, now=lambda: 1_000.0 + chat_key.KEEP_SECONDS + 1)
    assert not (tmp_path / "task" / "old.key").exists()
    assert (tmp_path / "task" / "new.key").exists()
