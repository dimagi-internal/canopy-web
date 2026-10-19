"""Files a person sends in a chat reach the agent on the cloud runner too.

This box used to ignore `origin_ref.attachments` entirely, so a screenshot sent
to a cloud-served chat never reached the agent and nothing said so. What these
pin:

- the bytes are fetched THROUGH THE TURN (`/runners/{rid}/turns/{tid}/attachments/…`),
  never through the chat's own read route, which this box's owner may not pass;
- the agent is told to READ the files that arrived, and told which did not;
- a confined session is not handed files outside its paths, but they are named.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from test_confined_turns import CAP, TID, _turn  # noqa: F401 — shared turn shape
from test_confined_turns import cr  # noqa: F401 — the turn-driving fixture


class _Net:
    """urlopen, stubbed: serves bytes for known ids, 404s the rest."""

    def __init__(self, ok: dict[str, bytes]):
        self.ok, self.urls = ok, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        aid = url.rsplit("/attachments/", 1)[1].split("/", 1)[0]
        if aid not in self.ok:
            raise urllib.error.HTTPError(url, 404, "attachment not found", {}, io.BytesIO(b""))
        return _Resp(self.ok[aid])


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _chat(**kw):
    return _turn(prompt="review the deck",
                 origin_ref={"chat_session_id": "s-1", "attachments": [
                     {"id": "a1", "filename": "slide-01.jpg"},
                     {"id": "a2", "filename": "slide-02.jpg"}]},
                 caller_context={"profile": "full", "capability": None}, **kw)


@pytest.fixture()
def net(cr, monkeypatch, tmp_path):  # noqa: F811
    mod, _ = cr
    monkeypatch.setattr(mod, "BASE_URL", "https://canopy.example")
    monkeypatch.setattr(mod, "ATTACHMENT_DIR", tmp_path / "attachments")
    stub = _Net({"a1": b"JPG1", "a2": b"JPG2"})
    monkeypatch.setattr(mod.urllib.request, "urlopen", stub)
    return stub


def test_a_chats_files_are_fetched_through_the_turn_and_read(cr, net, tmp_path):  # noqa: F811
    mod, calls = cr
    mod._run_turn("r-1", _chat())

    assert net.urls == [
        f"{mod.BASE_URL}/api/harness/runners/r-1/turns/{TID}/attachments/a1/content",
        f"{mod.BASE_URL}/api/harness/runners/r-1/turns/{TID}/attachments/a2/content",
    ]
    prompt = calls["exec"]["prompt"]
    assert prompt.startswith("review the deck")
    assert "Read them with the Read tool" in prompt
    shot = tmp_path / "attachments" / TID / "1-slide-01.jpg"
    assert str(shot) in prompt and shot.read_bytes() == b"JPG1"


def test_a_file_that_did_not_arrive_is_named_not_read(cr, net):  # noqa: F811
    mod, calls = cr
    net.ok.pop("a1")
    mod._run_turn("r-1", _chat())

    prompt = calls["exec"]["prompt"]
    read_list = prompt.split("before replying:")[1].split("\n\n")[0]
    assert "slide-02.jpg" in read_list and "slide-01.jpg" not in read_list
    assert "1 attached file could not be downloaded" in prompt and "slide-01.jpg" in prompt


def test_a_turn_with_no_attachments_is_untouched(cr, net):  # noqa: F811
    mod, calls = cr
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))

    assert net.urls == []
    assert calls["exec"]["prompt"] == "/ace:turn --thread 18c9"


def test_a_confined_session_is_told_of_files_it_is_not_given(cr, net):  # noqa: F811
    mod, calls = cr
    t = _turn(prompt="what is on this slide?",
              origin_ref={"chat_session_id": "s-1",
                          "attachments": [{"id": "a1", "filename": "slide-01.jpg"}]},
              caller_context={"profile": "restricted", "capability": CAP, "conversation": {}})
    mod._run_turn("r-1", t)

    assert net.urls == [], "a confined session reads only its capability's paths"
    prompt = calls["exec"]["prompt"]
    assert prompt.startswith("what is on this slide?")
    assert "slide-01.jpg" in prompt and "could not be downloaded" in prompt


def test_an_unfetchable_attachment_never_crashes_the_turn(cloud_runner, monkeypatch, tmp_path):
    """Found writing these tests: a malformed URL raised while the request was
    being BUILT, outside the network handler, and took down the turn's worker."""
    monkeypatch.setattr(cloud_runner, "BASE_URL", "")
    monkeypatch.setattr(cloud_runner, "ATTACHMENT_DIR", tmp_path / "attachments")
    paths, missing = cloud_runner._fetch_attachments(
        "r-1", {"id": TID, "origin_ref": {"attachments": [{"id": "a1", "filename": "x.png"}]}})
    assert paths == [] and missing == ["1-x.png"]


def test_a_hostile_filename_stays_under_the_attachment_dir(cloud_runner):
    assert cloud_runner._attachment_name("../../../../home/ubuntu/.ssh/authorized_keys") == "authorized_keys"
    assert cloud_runner._attachment_name("") == "attachment"
