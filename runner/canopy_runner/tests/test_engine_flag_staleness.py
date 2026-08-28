"""The runner's dissent from emdash's `agent_status`.

emdash reaches "working" only through Claude Code's UserPromptSubmit hook, and fires
`Stop` whenever the MAIN LOOP's turn ends — including a turn that ends only to hand off
to a background subagent. Nothing fires `start` again (the wake-up is a
task-notification), so the flag stays "completed" for the rest of the session while the
session churns on. These pin the discriminator that catches that: writes landing AFTER
the flag said the session stopped.
"""
import os

from canopy_runner import sessions as sessions_mod
from canopy_runner import transcript


class _Cfg:
    session_tail_count = 30
    emdash_db = ""      # no emdash DB here: resolution falls back to the convention


def _touch(path, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (when, when))


def _run(sessions, cfg=None, now=1_000_000.0):
    sessions_mod.annotate_engine_staleness(cfg or _Cfg(), sessions, now_fn=lambda: now)


def _session(status, task="t1"):
    return {"emdash_task": task, "project": "p", "agent_status": status}


def _resolve_to(monkeypatch, path):
    monkeypatch.setattr(transcript, "resolve_transcript", lambda *a, **k: path)


def test_subagent_writes_after_stop_override_a_completed_flag(tmp_path, monkeypatch):
    """The reported bug: a background hand-off pins the flag at "completed" while a
    subagent works. The subagent writes to its OWN transcript, never the parent's."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)

    s = _session("completed")
    _run([s])                      # tick 1: new flag value -> settle
    assert "agent_status_stale" not in s
    _run([s])                      # tick 2: baseline snapshotted
    assert "agent_status_stale" not in s

    # A subagent appends to <stem>/subagents/agent-*.jsonl. The parent file is untouched.
    _touch(tmp_path / "sess" / "subagents" / "agent-abc.jsonl", 999_990.0)
    s2 = _session("completed")
    _run([s2])
    assert s2["agent_status_stale"] is True


def test_a_genuinely_finished_turn_is_never_marked_stale(tmp_path, monkeypatch):
    """The property the engine flag was adopted FOR: the badge retires the moment a
    turn ends. A real end writes nothing more, so it never clears the bar."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_999.0)      # the turn's own closing write
    _resolve_to(monkeypatch, parent)
    for _ in range(5):
        s = _session("completed")
        _run([s])
        assert "agent_status_stale" not in s


def test_the_turns_own_closing_write_is_inside_the_baseline(tmp_path, monkeypatch):
    """Why the baseline is snapshotted a tick LATER than the flag change: Stop fires
    before the last record is flushed, so a same-instant baseline would read that
    flush as new work and call every finished session running."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)
    s = _session("completed")
    _run([s])                                  # flag change observed; settling
    _touch(parent, 999_001.0)                  # the closing flush lands now
    _run([s])                                  # baseline includes it
    s2 = _session("completed")
    _run([s2])
    assert "agent_status_stale" not in s2


def test_dissent_de_latches_once_the_writing_stops(tmp_path, monkeypatch):
    """A second Stop writes the SAME flag value, so the baseline never re-settles.
    Without the quiet window a session that woke up, worked and then genuinely
    finished would claim to be running forever."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)
    s = _session("completed")
    _run([s]); _run([s])                       # settle + baseline
    _touch(parent, 999_500.0)                  # woke back up and worked
    s2 = _session("completed")
    _run([s2], now=999_600.0)
    assert s2["agent_status_stale"] is True
    s3 = _session("completed")
    _run([s3], now=999_500.0 + sessions_mod.STILL_WRITING_SECONDS + 1)
    assert "agent_status_stale" not in s3


def test_a_working_flag_is_left_alone_and_forgets_its_watch(tmp_path, monkeypatch):
    """One-directional: this can only ever say "still running". A `working` flag needs
    no help, and clearing the watch means the next Stop settles a fresh baseline."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)
    s = _session("completed")
    _run([s]); _run([s])
    _run([_session("working")])
    assert sessions_mod._ENGINE_FLAG == {}


def test_a_blank_flag_is_left_to_the_servers_own_fallback(tmp_path, monkeypatch):
    """"" means the runner could not answer — a cloud runner, a drifted schema. The
    server falls back to activity recency for those; dissenting would be a guess."""
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)
    s = _session("")
    _run([s]); _touch(parent, 999_900.0); _run([s])
    assert "agent_status_stale" not in s
    assert sessions_mod._ENGINE_FLAG == {}


def test_an_unresolvable_transcript_never_claims_to_be_writing(tmp_path, monkeypatch):
    sessions_mod._ENGINE_FLAG.clear()
    _resolve_to(monkeypatch, None)
    s = _session("awaiting-input")
    for _ in range(3):
        _run([s])
    assert "agent_status_stale" not in s


def test_a_vanished_session_drops_its_watch(tmp_path, monkeypatch):
    sessions_mod._ENGINE_FLAG.clear()
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 999_000.0)
    _resolve_to(monkeypatch, parent)
    _run([_session("completed")])
    assert "t1" in sessions_mod._ENGINE_FLAG
    _run([])
    assert sessions_mod._ENGINE_FLAG == {}


def test_activity_mtime_takes_the_newest_of_parent_and_subagents(tmp_path):
    parent = tmp_path / "sess.jsonl"
    _touch(parent, 500.0)
    assert transcript.activity_mtime(parent) == 500.0
    _touch(tmp_path / "sess" / "subagents" / "agent-1.jsonl", 900.0)
    _touch(tmp_path / "sess" / "subagents" / "agent-2.jsonl", 700.0)
    assert transcript.activity_mtime(parent) == 900.0
    assert transcript.activity_mtime(None) == 0.0
    assert transcript.activity_mtime(tmp_path / "nope.jsonl") == 0.0
