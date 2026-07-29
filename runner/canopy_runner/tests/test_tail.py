"""Incremental byte-offset transcript reader — reads only the new tail each call."""
import json

from canopy_runner.tail import TailReader


def _line(d):
    return json.dumps(d) + "\n"


def test_read_new_is_incremental(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line({"type": "user", "message": {"content": "a"}}))
    r = TailReader(p)
    assert [x["type"] for x in r.read_new()] == ["user"]
    assert r.read_new() == []  # nothing new -> no re-read
    with open(p, "a") as f:
        f.write(_line({"type": "assistant", "message": {"content": "b"}}))
    assert [x["type"] for x in r.read_new()] == ["assistant"]  # only the NEW record


def test_partial_trailing_line_is_buffered(tmp_path):
    p = tmp_path / "t.jsonl"
    r = TailReader(p)
    p.write_text('{"type":"user","message"')  # mid-append, no newline
    assert r.read_new() == []  # incomplete -> not parsed, buffered
    with open(p, "a") as f:
        f.write(':{"content":"hi"}}\n')  # completes the line
    recs = r.read_new()
    assert recs and recs[0]["type"] == "user"


def test_seek_end_skips_history(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line({"type": "user", "message": {"content": "old"}})
                 + _line({"type": "assistant", "message": {"content": "old2"}}))
    r = TailReader(p)
    r.seek_end()
    assert r.read_new() == []  # existing history skipped
    with open(p, "a") as f:
        f.write(_line({"type": "assistant", "message": {"content": "new"}}))
    recs = r.read_new()
    assert recs and recs[0]["message"]["content"] == "new"


def test_truncation_resets(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line({"type": "user", "message": {"content": "a"}})
                 + _line({"type": "user", "message": {"content": "b"}}))
    r = TailReader(p)
    r.read_new()
    p.write_text(_line({"type": "user", "message": {"content": "fresh"}}))  # smaller -> rotated
    recs = r.read_new()
    assert recs and recs[0]["message"]["content"] == "fresh"


def test_missing_file_is_safe(tmp_path):
    r = TailReader(tmp_path / "nope.jsonl")
    assert r.read_new() == []
    r.seek_end()
    assert r.read_new() == []
