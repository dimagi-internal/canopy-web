import json
from pathlib import Path

from canopy_runner import transcript


def test_encode_project_dir_replaces_slash_and_dot():
    wt = Path("/Users/j/emdash/worktrees/canopy-web/emdash/mobile-kn063")
    assert transcript.encode_project_dir(wt) == (
        "-Users-j-emdash-worktrees-canopy-web-emdash-mobile-kn063"
    )
    # dots also become dashes (Claude Code's convention)
    assert transcript.encode_project_dir(Path("/a/b.c")) == "-a-b-c"


def _write_transcript(claude_home: Path, worktree: Path, lines: list[dict]) -> Path:
    proj = claude_home / transcript.encode_project_dir(worktree)
    proj.mkdir(parents=True)
    f = proj / "sess.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), "utf-8")
    return f


def test_session_tail_reads_recent_user_and_assistant_text(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    worktree = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    _write_transcript(claude_home, worktree, [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "user", "message": {"content": "fix the header"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "On it."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
    ])
    msgs, reason = transcript.session_tail(
        "canopy-web", "ddd", limit=8, home=home, claude_home=claude_home
    )
    assert reason == ""
    # Clean conversation only: the human prompt and the AI's text — the tool_use
    # turn is dropped, not rendered as "[tool: Edit]".
    assert msgs == [
        {"role": "user", "text": "fix the header"},
        {"role": "assistant", "text": "On it."},
    ]


def test_read_recent_messages_drops_tool_calls_and_results_and_noise(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "run the tests"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Running them now."},
            {"type": "tool_use", "name": "Bash"},  # dropped, but the text survives
        ]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},  # dropped entirely
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "exit 0"}]}},  # dropped
        {"type": "user", "message": {"content": "<task-notification>\nfoo</task-notification>"}},  # harness noise, dropped
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "All green."}]}},
    ]), "utf-8")
    msgs = transcript.read_recent_messages(f, limit=8)
    assert msgs == [
        {"role": "user", "text": "run the tests"},
        {"role": "assistant", "text": "Running them now."},
        {"role": "assistant", "text": "All green."},
    ]


def test_session_tail_limits_to_last_n(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    worktree = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    lines = [{"type": "system", "subtype": "init", "session_id": "s1"}]
    for i in range(20):
        lines.append({"type": "user", "message": {"content": f"m{i}"}})
    _write_transcript(claude_home, worktree, lines)
    msgs, reason = transcript.session_tail(
        "canopy-web", "ddd", limit=8, home=home, claude_home=claude_home
    )
    assert reason == ""
    assert [m["text"] for m in msgs] == [f"m{i}" for i in range(12, 20)]


def test_session_tail_missing_transcript_returns_reason_not_raise(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    msgs, reason = transcript.session_tail(
        "canopy-web", "nope", limit=8, home=home, claude_home=claude_home
    )
    assert msgs == []
    assert reason == "no-transcript"


def test_read_recent_messages_skips_malformed_lines(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text(
        '{"type":"user","message":{"content":"ok"}}\n'
        "NOT JSON\n"
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        "utf-8",
    )
    msgs = transcript.read_recent_messages(f, limit=8)
    assert [m["text"] for m in msgs] == ["ok", "hi"]


def test_read_recent_messages_skips_sidechain(tmp_path):
    # Subagent (Task tool) turns are recorded as top-level entries with
    # "isSidechain": true — the recent-message tail is meant to show the
    # MAIN conversation thread, so these must be excluded.
    f = tmp_path / "x.jsonl"
    f.write_text(
        '{"type":"user","message":{"content":"main ask"}}\n'
        '{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"text","text":"subagent noise"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"main reply"}]}}\n',
        "utf-8",
    )
    msgs = transcript.read_recent_messages(f, limit=8)
    assert [m["text"] for m in msgs] == ["main ask", "main reply"]


def test_text_truncated_to_max(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"content": "z" * 5000}}), "utf-8")
    msgs = transcript.read_recent_messages(f, limit=8)
    assert len(msgs[0]["text"]) == transcript.MAX_MSG_CHARS


def test_read_recent_messages_skips_wrong_shape_message_field(tmp_path):
    # Valid JSON, but "message" is a str instead of a dict — .get() on it
    # would raise AttributeError if not guarded. Surrounding good messages
    # must still come through.
    f = tmp_path / "x.jsonl"
    f.write_text(
        '{"type":"user","message":{"content":"ok"}}\n'
        '{"type":"user","message":"not-a-dict"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        "utf-8",
    )
    msgs = transcript.read_recent_messages(f, limit=8)
    assert [m["text"] for m in msgs] == ["ok", "hi"]


def test_read_recent_messages_skips_bare_json_list_line(tmp_path):
    # A line that is valid JSON but not an object at all (a bare list) —
    # payload.get(...) would raise AttributeError if not guarded.
    f = tmp_path / "x.jsonl"
    f.write_text(
        '{"type":"user","message":{"content":"ok"}}\n'
        "[1,2,3]\n"
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        "utf-8",
    )
    msgs = transcript.read_recent_messages(f, limit=8)
    assert [m["text"] for m in msgs] == ["ok", "hi"]


def test_session_tail_wrong_shape_only_returns_empty_transcript_not_raise(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    worktree = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    _write_transcript(claude_home, worktree, [
        {"type": "system", "subtype": "init", "session_id": "s1"},
    ])
    # Overwrite with content lines that are all wrong-shape (valid JSON,
    # bad shape) — session_tail must degrade to ([], "empty-transcript"),
    # never raise.
    proj = claude_home / transcript.encode_project_dir(worktree)
    f = proj / "sess.jsonl"
    f.write_text(
        '{"type":"user","message":"not-a-dict"}\n'
        "[1,2,3]\n",
        "utf-8",
    )
    msgs, reason = transcript.session_tail(
        "canopy-web", "ddd", limit=8, home=home, claude_home=claude_home
    )
    assert msgs == []
    assert reason == "empty-transcript"


def test_attach_recent_tail_fills_multiple_top_sessions(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    for task, text in [("ddd", "hello ddd"), ("turn", "hello turn")]:
        wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / task
        _write_transcript(claude_home, wt, [
            {"type": "user", "message": {"content": text}},
        ])
    sessions = [
        {"emdash_task": "ddd", "project": "canopy-web", "status": "in_progress"},
        {"emdash_task": "turn", "project": "canopy-web", "status": "in_progress"},
    ]
    transcript.attach_recent_tail(sessions, home=home, claude_home=claude_home)
    assert sessions[0]["recent_messages"] == [{"role": "user", "text": "hello ddd"}]
    assert sessions[1]["recent_messages"] == [{"role": "user", "text": "hello turn"}]


def test_attach_recent_tail_respects_count_cap(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    for task in ("a", "b", "c"):
        wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / task
        _write_transcript(claude_home, wt, [
            {"type": "user", "message": {"content": task}},
        ])
    sessions = [{"emdash_task": t, "project": "canopy-web"} for t in ("a", "b", "c")]
    transcript.attach_recent_tail(sessions, count=2, home=home, claude_home=claude_home)
    assert sessions[0]["recent_messages"] == [{"role": "user", "text": "a"}]
    assert sessions[1]["recent_messages"] == [{"role": "user", "text": "b"}]
    assert "recent_messages" not in sessions[2]  # beyond count -> untouched


def test_attach_recent_tail_empty_list_is_noop(tmp_path):
    sessions: list[dict] = []
    transcript.attach_recent_tail(sessions, home=tmp_path)  # must not raise
    assert sessions == []


def test_attach_recent_tail_missing_transcript_sets_empty(tmp_path):
    sessions = [{"emdash_task": "nope", "project": "canopy-web"}]
    transcript.attach_recent_tail(sessions, home=tmp_path, claude_home=tmp_path)
    assert sessions[0]["recent_messages"] == []


def _write_at(claude_home: Path, worktree: Path, lines: list[dict]) -> Path:
    """Write a transcript at an ARBITRARY worktree path (helper for layout tests)."""
    proj = claude_home / transcript.encode_project_dir(worktree)
    proj.mkdir(parents=True)
    f = proj / "sess.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), "utf-8")
    return f


def test_resolve_transcript_matches_random_dedupe_suffix(tmp_path):
    # emdash's real dir carries a random suffix ("-cysov"); the DB task name does not.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    worktree = home / "emdash" / "worktrees" / "ace" / "emdash" / "ace-demo-1352-cysov"
    _write_at(claude_home, worktree, [{"type": "user", "message": {"content": "hi"}}])
    msgs, reason = transcript.session_tail(
        "ace", "ace-demo-1352", limit=8, home=home, claude_home=claude_home
    )
    assert reason == ""
    assert msgs == [{"role": "user", "text": "hi"}]


def test_resolve_transcript_matches_layout_without_emdash_segment(tmp_path):
    # Some worktrees (e.g. echo's) sit at <repo>/<task> with no `emdash` segment.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    worktree = home / "emdash" / "worktrees" / "echo" / "spotty-cities-mix"
    _write_at(claude_home, worktree, [{"type": "user", "message": {"content": "yo"}}])
    msgs, reason = transcript.session_tail(
        "echo", "spotty-cities-mix", limit=8, home=home, claude_home=claude_home
    )
    assert reason == ""
    assert msgs == [{"role": "user", "text": "yo"}]


def test_resolve_transcript_does_not_grab_a_sibling_task_prefix(tmp_path):
    # Anti-collision: task "mobile" must not resolve to sibling "alt-mobile"'s dir.
    # The prefix is anchored at the parent segment (...-emdash-mobile), so a dir
    # whose leaf is "alt-mobile" (…-emdash-alt-mobile) can't match.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    sibling = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "alt-mobile"
    _write_at(claude_home, sibling, [{"type": "user", "message": {"content": "sibling"}}])
    msgs, reason = transcript.session_tail(
        "canopy-web", "mobile", limit=8, home=home, claude_home=claude_home
    )
    assert msgs == []
    assert reason == "no-transcript"


def test_read_recent_messages_reads_only_tail_of_large_file(tmp_path):
    # A transcript larger than TAIL_BYTES: the reader seeks to the end and must
    # still return the final message (proving it doesn't need the whole file).
    f = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1000}})
    n = transcript.TAIL_BYTES // len(filler) + 50  # comfortably exceed the tail window
    lines = [filler] * n
    lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "LAST"}]}}))
    f.write_text("\n".join(lines), "utf-8")
    assert f.stat().st_size > transcript.TAIL_BYTES
    msgs = transcript.read_recent_messages(f, limit=3)
    assert msgs[-1] == {"role": "assistant", "text": "LAST"}
    assert len(msgs) <= 3


def test_attach_recent_tail_sets_last_active_from_newest_transcript_record(tmp_path):
    # The live-run case PR #300 fixed: the CC session is what's active, and emdash's
    # own UI timestamp is stale. The newest record IN the transcript is the signal.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    _write_transcript(claude_home, wt, [
        {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-07-25T10:00:00.000Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "yo"}]},
         "timestamp": "2026-07-25T10:04:00.000Z"},
    ])
    sessions = [{"emdash_task": "ddd", "project": "canopy-web",
                 "last_interacted_at": "2020-01-01 00:00:00"}]  # stale emdash value
    transcript.attach_recent_tail(sessions, home=home, claude_home=claude_home)
    assert sessions[0]["last_interacted_at"] == "2026-07-25T10:04:00+00:00"


def test_attach_recent_tail_ignores_a_touched_file_with_no_new_records(tmp_path):
    # THE BUG (labs 2026-07-25): a live-but-IDLE `claude` process rewrites its own
    # transcript periodically without appending a single conversational record, so
    # the file's mtime keeps advancing while nothing happens. Reading mtime as the
    # activity signal made dormant sessions creep to the top of the phone's list
    # reading "just now" (and, via is_session_running's 120s window, even render as
    # "running"). Observed skews on the live fleet: mtime 23.4h and 44.7h newer than
    # the newest record in the file. Activity is what the transcript SAYS, not when
    # the file was last written.
    import os
    import time
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "dormant"
    f = _write_transcript(claude_home, wt, [
        {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-07-24T09:00:00.000Z"},
    ])
    os.utime(f, (time.time(), time.time()))  # touched now; content unchanged
    sessions = [{"emdash_task": "dormant", "project": "canopy-web",
                 "last_interacted_at": "2026-07-24 09:00:00"}]
    transcript.attach_recent_tail(sessions, home=home, claude_home=claude_home)
    # Still the last real record — the touch bought the session no freshness.
    assert transcript._parse_utc(sessions[0]["last_interacted_at"]) == transcript._parse_utc(
        "2026-07-24T09:00:00Z"
    )


def test_attach_recent_tail_keeps_a_newer_emdash_timestamp(tmp_path):
    # The two clocks measure different halves of one session: emdash's value tracks
    # its own UI, the transcript tracks the CC session. Neither dominates, so take
    # the later — here emdash saw an interaction the transcript hasn't recorded yet.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    _write_transcript(claude_home, wt, [
        {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-07-25T10:00:00.000Z"},
    ])
    sessions = [{"emdash_task": "ddd", "project": "canopy-web",
                 "last_interacted_at": "2026-07-25 11:30:00"}]
    transcript.attach_recent_tail(sessions, home=home, claude_home=claude_home)
    assert sessions[0]["last_interacted_at"] == "2026-07-25 11:30:00"


def test_attach_recent_tail_leaves_timestamp_alone_when_transcript_has_none(tmp_path):
    # A transcript with no parseable timestamps yields no opinion — emdash's value
    # stands rather than being replaced by a guess.
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    wt = home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "ddd"
    _write_transcript(claude_home, wt, [{"type": "user", "message": {"content": "hi"}}])
    sessions = [{"emdash_task": "ddd", "project": "canopy-web",
                 "last_interacted_at": "2020-01-01 00:00:00"}]
    transcript.attach_recent_tail(sessions, home=home, claude_home=claude_home)
    assert sessions[0]["last_interacted_at"] == "2020-01-01 00:00:00"


def test_newest_record_time_takes_the_max_not_the_last_line(tmp_path):
    # Records are appended in order in practice, but a rewrite could land them out
    # of order; the newest wins either way.
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": "2026-07-25T12:00:00.000Z"},
        {"type": "system", "timestamp": "2026-07-25T11:00:00.000Z"},
        {"type": "assistant"},           # no timestamp — skipped
        {"type": "user", "timestamp": "nonsense"},  # unparseable — skipped
    ]), "utf-8")
    assert transcript.newest_record_time(f) == "2026-07-25T12:00:00+00:00"


def test_newest_record_time_ignores_bookkeeping_records(tmp_path):
    # The parked-session shape measured on the fleet (spark, 2026-08-15): the agent
    # stopped at 12:00 and the transcript kept growing with a PR link and background
    # task-queue churn. Counting those made a 27-minute-parked session read as
    # "just acted" in the chat list, and float to the top of its running-first sort.
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "timestamp": "2026-08-15T12:00:00.000Z"},
        {"type": "pr-link", "timestamp": "2026-08-15T12:24:00.000Z"},
        {"type": "queue-operation", "timestamp": "2026-08-15T12:27:00.000Z"},
    ]), "utf-8")
    assert transcript.newest_record_time(f) == "2026-08-15T12:00:00+00:00"


def test_newest_record_time_still_counts_non_conversational_turn_records(tmp_path):
    # The opposite guard: an `attachment` or `system` row mid-tool-loop IS the
    # session working, and is exactly what keeps a long tool call from reading as
    # dead. Only the bookkeeping kinds were meant to be dropped.
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "timestamp": "2026-08-15T12:00:00.000Z"},
        {"type": "attachment", "timestamp": "2026-08-15T12:05:00.000Z"},
        {"type": "system", "timestamp": "2026-08-15T12:06:00.000Z"},
    ]), "utf-8")
    assert transcript.newest_record_time(f) == "2026-08-15T12:06:00+00:00"


def test_newest_record_time_of_an_unreadable_file_is_none(tmp_path):
    assert transcript.newest_record_time(tmp_path / "missing.jsonl") is None
