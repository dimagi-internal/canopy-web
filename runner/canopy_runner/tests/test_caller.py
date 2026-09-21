"""The caller envelope reaches the agent as a file, and a command prompt names it."""
import json
import os
import stat

from canopy_runner import caller

TID = "3f2b8c1e-0000-4000-8000-000000000001"
ENV = {"version": 1, "turn_id": TID, "who": {"kind": "contact"}, "verified": True}


def test_writes_the_envelope_privately(tmp_path):
    root = tmp_path / "caller"
    p = caller.write_caller_file({"id": TID, "caller_context": ENV}, root=root)
    assert p == root / f"{TID}.json"
    assert json.loads(p.read_text()) == ENV
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_an_old_server_without_an_envelope_writes_nothing(tmp_path):
    assert caller.write_caller_file({"id": TID}, root=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_a_turn_id_that_is_not_a_uuid_is_never_joined_onto_a_path(tmp_path):
    assert caller.write_caller_file(
        {"id": "../../.ssh/authorized_keys", "caller_context": ENV}, root=tmp_path) is None


def test_old_envelopes_are_pruned(tmp_path):
    stale = tmp_path / "old.json"
    stale.write_text("{}")
    os.utime(stale, (0, 0))
    caller.write_caller_file({"id": TID, "caller_context": ENV}, root=tmp_path,
                             now=lambda: caller.KEEP_SECONDS + 10)
    assert not stale.exists()


def test_a_slash_command_gets_the_flag(tmp_path):
    p = tmp_path / "x.json"
    assert caller.with_caller_flag("/ace:turn --thread t1", p) == f"/ace:turn --thread t1 --caller {p}"


def test_the_flag_goes_on_the_command_line_not_after_rehydrated_context(tmp_path):
    p = tmp_path / "x.json"
    out = caller.with_caller_flag("/ace:turn --thread t1\nmore", p)
    assert out.splitlines()[0].endswith(f"--caller {p}")


def test_a_persons_words_are_never_touched(tmp_path):
    words = "which opportunities are behind on payments?"
    assert caller.with_caller_flag(words, tmp_path / "x.json") == words


def test_no_path_no_flag_and_never_twice(tmp_path):
    assert caller.with_caller_flag("/ace:turn", None) == "/ace:turn"
    once = caller.with_caller_flag("/ace:turn", tmp_path / "x.json")
    assert caller.with_caller_flag(once, tmp_path / "x.json") == once
