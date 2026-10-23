"""The laptop's part in a transfer's work (transfer.py): pack as the source, close
the task once the target has it, restore as the target. Packing itself is
test_handoff.py; canopy-web's flow is tests/test_session_handoff.py."""
from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from canopy_runner import close as close_mod
from canopy_runner import handoff, transfer


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeClient:
    def __init__(self, due=(), archive=b""):
        self.due, self.archive, self.calls = list(due), archive, []

    def sync_handoffs(self, runner_id):
        return self.due

    def upload_handoff(self, runner_id, hid, archive, chunks):
        self.calls.append(("upload", hid, b"".join(chunks)))

    def post_handoff_captured(self, runner_id, hid, *, ok, manifest=None, error=""):
        self.calls.append(("captured", hid, ok, manifest, error))

    def post_handoff_close(self, runner_id, hid, *, ok, error=""):
        self.calls.append(("close", hid, ok, error))

    def download_handoff(self, runner_id, hid):
        return self.archive

    def post_handoff_restored(self, runner_id, hid, report):
        self.calls.append(("restored", hid, report))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e.com"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@e.com")):
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = tmp_path / "task-wt"
    git(tmp_path, "clone", "-q", str(origin), str(src))
    (src / "a.py").write_text("1\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "init")
    git(src, "push", "-q", "origin", "main")
    git(src, "checkout", "-q", "-b", "wip")
    (src / "a.py").write_text("2\n")
    (src / "b.txt").write_text("new\n")
    return SimpleNamespace(origin=origin, src=src, tmp=tmp_path)


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(runner_id="r1", emdash_db=str(tmp_path / "none.db"), cdp_port=9222)


def test_the_source_packs_the_tasks_worktree_and_uploads_it(repo, cfg, monkeypatch):
    monkeypatch.setattr(transfer, "source_worktree", lambda c, key, project: repo.src)
    client = FakeClient(due=[{"id": "h1", "session_key": "export-audit", "project": "canopy-web",
                              "capture": True, "close": False, "transcript": False}])
    transfer.drain(cfg, client)
    (_, _, uploaded), (_, _, ok, manifest, _) = client.calls
    assert ok and manifest["branch"] == "wip" and manifest["sha256"]
    target = repo.tmp / "target"
    git(repo.tmp, "clone", "-q", str(repo.origin), str(target))
    git(target, "checkout", "-q", "--detach")
    assert handoff.restore(target, uploaded)["ok"]


def test_a_task_with_no_worktree_says_so(cfg, monkeypatch):
    monkeypatch.setattr(transfer, "source_worktree", lambda c, key, project: None)
    client = FakeClient(due=[{"id": "h2", "session_key": "gone", "capture": True}])
    transfer.drain(cfg, client)
    (_, _, ok, _, error), = client.calls
    assert not ok and "no worktree for 'gone'" in error


def test_the_source_closes_the_task_through_the_reliable_close_path(cfg, monkeypatch):
    closed = []
    monkeypatch.setattr(close_mod, "close_session",
                        lambda key, **kw: closed.append((key, kw["project"])) or "deleted")
    client = FakeClient(due=[{"id": "h3", "session_key": "export-audit", "project": "canopy-web",
                              "capture": False, "close": True}])
    transfer.drain(cfg, client)
    assert closed == [("export-audit", "canopy-web")]
    assert client.calls == [("close", "h3", True, "")]


def test_a_failed_close_is_reported_with_its_reason_once(cfg, monkeypatch):
    def boom(key, **kw):
        raise close_mod.CloseFailed("still there")

    monkeypatch.setattr(close_mod, "close_session", boom)
    item = {"id": "h4", "session_key": "export-audit", "project": "canopy-web", "close": True}
    client = FakeClient(due=[item])
    transfer.drain(cfg, client)
    (_, _, ok, reason), = client.calls
    assert not ok and "sidebar did not show it" in reason
    # Retried next tick; the same reason is not sent again.
    client2 = FakeClient(due=[{**item, "close_error": reason}])
    transfer.drain(cfg, client2)
    assert client2.calls == []


def test_the_target_restores_writes_the_report_and_says_so(repo, cfg):
    archive, _ = handoff.capture(repo.src)
    target = repo.tmp / "target"
    git(repo.tmp, "clone", "-q", str(repo.origin), str(target))
    git(target, "checkout", "-q", "--detach")
    client = FakeClient(archive=archive)
    turn = {"origin_ref": {"handoff": {"id": "h5", "resume": False}}}
    report = transfer.restore_into(cfg, client, turn, target)
    assert report["ok"]
    assert json.loads(handoff.marker_path(target).read_text())["ok"] is True
    assert client.calls[-1][0:2] == ("restored", "h5")


def test_a_turn_without_a_handoff_restores_nothing(cfg, tmp_path):
    client = FakeClient()
    assert transfer.restore_into(cfg, client, {"origin_ref": {}}, tmp_path) is None
    assert client.calls == []


def test_chunks_reassemble():
    data = bytes(range(256)) * 20000
    parts = handoff.chunks(data, size=100_000)
    assert b"".join(parts) == data and all(len(base64.b64encode(p)) < 2_200_000 for p in parts)
