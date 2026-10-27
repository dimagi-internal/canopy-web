"""The runner half of chat attachments: fetch the bytes, then make sure the agent
actually looks at them.

The feature is worthless if the file reaches disk and the agent never opens it,
so both halves are pinned here.
"""
from __future__ import annotations
import sys

import pathlib

import pytest

from canopy_runner import execute


class _Client:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.fetched: list[str] = []

    def download_attachment(self, runner_id, turn_id, attachment_id, dest: pathlib.Path):
        if attachment_id in self.fail:
            raise RuntimeError("boom")
        # Asked for as THIS runner, for THIS turn — the server authorizes by the
        # claim, not by whether the box's owner can read the chat.
        self.fetched.append((runner_id, turn_id, attachment_id))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PNGDATA")


def _turn(refs, turn_id="t1"):
    return {"id": turn_id, "origin_ref": {"attachments": refs}}


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "ATTACHMENT_ROOT", tmp_path / "attachments")


def test_a_turn_with_no_attachments_fetches_nothing():
    client = _Client()
    assert execute.fetch_attachments(client, "r1", {"id": "t1", "origin_ref": {}}) == ([], [])
    assert client.fetched == []


def test_attachments_land_on_disk_under_the_turn():
    client = _Client()
    paths, missing = execute.fetch_attachments(client, "r1", _turn([{"id": "a1", "filename": "shot.png"}]))

    assert client.fetched == [("r1", "t1", "a1")]
    assert missing == []
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"PNGDATA"
    assert paths[0].name == "1-shot.png"
    assert "t1" in str(paths[0]), "scoped per turn so two sends cannot collide"


def test_a_hostile_filename_cannot_escape_the_attachment_root(tmp_path):
    """A runner must never trust a path handed to it over the wire."""
    client = _Client()
    paths, _ = execute.fetch_attachments(
        client, "r1", _turn([{"id": "a1", "filename": "../../../../.ssh/authorized_keys"}])
    )

    assert len(paths) == 1
    assert ".." not in str(paths[0])
    assert str(paths[0]).startswith(str(execute.ATTACHMENT_ROOT))


def test_one_unfetchable_attachment_does_not_sink_the_turn():
    """The human's TEXT is usually the substance — answering without the image
    beats never answering."""
    client = _Client(fail={"bad"})
    paths, missing = execute.fetch_attachments(
        client, "r1", _turn([{"id": "bad", "filename": "x.png"}, {"id": "ok", "filename": "y.png"}])
    )

    assert [p.name for p in paths] == ["2-y.png"]
    assert missing == ["1-x.png"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path literals ('/tmp/a/shot.png'); Windows renders the same Path with backslashes")
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
    paths, _ = execute.fetch_attachments(client, "r1", _turn([{"id": "bad", "filename": "gone.png"}]))
    out = execute.prompt_with_attachments("hi", paths)

    assert out == "hi"
    assert "gone.png" not in out


def test_a_file_that_never_arrived_is_named_so_the_agent_can_say_so():
    """Silently dropping the images is how a whole deck review went wrong
    (2026-10-15: 63 images 404'd, the agent got text only and could not tell
    why). The agent must be told what is missing — but never told to READ it."""
    client = _Client(fail={"bad"})
    paths, missing = execute.fetch_attachments(
        client, "r1", _turn([{"id": "bad", "filename": "slide-01.jpg"}, {"id": "ok", "filename": "slide-02.jpg"}])
    )
    out = execute.prompt_with_attachments("review the deck", paths, missing)

    assert "1 attached file could not be downloaded" in out
    assert "slide-01.jpg" in out
    read_list = out.split("before replying:")[1].split("\n\n")[0]
    assert "slide-02.jpg" in read_list and "slide-01.jpg" not in read_list


def test_a_long_list_of_missing_files_is_summarized():
    out = execute.prompt_with_attachments("hi", [], [f"s{i}.jpg" for i in range(29)])

    assert "29 attached files could not be downloaded" in out
    assert "and 19 more" in out


def test_two_pasted_screenshots_named_alike_both_arrive(tmp_path, monkeypatch):
    """Every pasted screenshot is "image.png"; two in one message used to land on
    one path, the second overwriting the first."""
    monkeypatch.setattr(execute, "ATTACHMENT_ROOT", tmp_path)
    client = _Client()
    paths, missing = execute.fetch_attachments(
        client, "r1", _turn([{"id": "a1", "filename": "image.png"}, {"id": "a2", "filename": "image.png"}]))
    assert missing == [] and [p.name for p in paths] == ["1-image.png", "2-image.png"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path literals; Windows renders the same Path with backslashes")
def test_a_placeholder_in_the_message_labels_its_file():
    """The composer types "[Image #N]" where a file was pasted. The message stays
    as typed; the note says which file each placeholder is."""
    paths = [pathlib.Path("/tmp/t/1-image.png"), pathlib.Path("/tmp/t/2-image.png")]
    out = execute.prompt_with_attachments("before [Image #2] and after", paths)
    assert out.startswith("before [Image #2] and after\n\n")
    assert "- /tmp/t/1-image.png\n- [Image #2] /tmp/t/2-image.png" in out
    assert "marks where the user placed" in out
    assert "marks where" not in execute.prompt_with_attachments("no placeholder", paths)
