"""The runner half of chat attachments: fetch the bytes, then make sure the agent
actually looks at them.

The feature is worthless if the file reaches disk and the agent never opens it,
so both halves are pinned here.
"""
from __future__ import annotations

import pathlib

import pytest

from canopy_runner import execute


class _Client:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.fetched: list[str] = []

    def download_attachment(self, attachment_id, dest: pathlib.Path):
        if attachment_id in self.fail:
            raise RuntimeError("boom")
        self.fetched.append(attachment_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PNGDATA")


def _turn(refs, turn_id="t1"):
    return {"id": turn_id, "origin_ref": {"attachments": refs}}


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "ATTACHMENT_ROOT", tmp_path / "attachments")


def test_a_turn_with_no_attachments_fetches_nothing():
    client = _Client()
    assert execute.fetch_attachments(client, {"id": "t1", "origin_ref": {}}) == []
    assert client.fetched == []


def test_attachments_land_on_disk_under_the_turn():
    client = _Client()
    paths = execute.fetch_attachments(client, _turn([{"id": "a1", "filename": "shot.png"}]))

    assert len(paths) == 1
    assert paths[0].read_bytes() == b"PNGDATA"
    assert paths[0].name == "shot.png"
    assert "t1" in str(paths[0]), "scoped per turn so two sends cannot collide"


def test_a_hostile_filename_cannot_escape_the_attachment_root(tmp_path):
    """A runner must never trust a path handed to it over the wire."""
    client = _Client()
    paths = execute.fetch_attachments(
        client, _turn([{"id": "a1", "filename": "../../../../.ssh/authorized_keys"}])
    )

    assert len(paths) == 1
    assert ".." not in str(paths[0])
    assert str(paths[0]).startswith(str(execute.ATTACHMENT_ROOT))


def test_one_unfetchable_attachment_does_not_sink_the_turn():
    """The human's TEXT is usually the substance — answering without the image
    beats never answering."""
    client = _Client(fail={"bad"})
    paths = execute.fetch_attachments(
        client, _turn([{"id": "bad", "filename": "x.png"}, {"id": "ok", "filename": "y.png"}])
    )

    assert [p.name for p in paths] == ["y.png"]


def test_the_prompt_tells_the_agent_to_READ_the_file():
    """An agent merely told a file exists keeps talking; one told to read it
    reaches for the tool. Looking at the screenshot is the entire feature."""
    out = execute.prompt_with_attachments("what is this?", [pathlib.Path("/tmp/a/shot.png")])

    assert "what is this?" in out
    assert "/tmp/a/shot.png" in out
    assert "Read tool" in out


def test_the_prompt_is_untouched_when_there_are_no_attachments():
    assert execute.prompt_with_attachments("plain", []) == "plain"


def test_only_files_that_actually_arrived_are_cited():
    """The prompt must never name a file that failed to download — the agent
    would try to read it and derail on a missing path."""
    client = _Client(fail={"bad"})
    paths = execute.fetch_attachments(client, _turn([{"id": "bad", "filename": "gone.png"}]))
    out = execute.prompt_with_attachments("hi", paths)

    assert out == "hi"
    assert "gone.png" not in out
