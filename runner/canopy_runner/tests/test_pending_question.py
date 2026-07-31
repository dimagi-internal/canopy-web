"""The blocked-agent dialog, attached to the session report.

The parse itself is `canopy_transcript.pending_question` (tested there against
the real `spark` payload). What is tested here is the half that made the bug:
WHICH sessions get asked, and whether asking can ever cost the report.
"""
import json
from pathlib import Path

from canopy_runner import transcript

ASK = {
    "type": "assistant",
    "message": {"content": [{
        "type": "tool_use", "id": "toolu_01", "name": "AskUserQuestion",
        "input": {"questions": [{
            "question": "How should the run proceed?",
            "header": "Phase 3→4",
            "options": [{"label": "Proceed", "description": "carry on"},
                        {"label": "Stop", "description": "end here"}],
        }]},
    }]},
}
ANSWER = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                             "content": "answered"}]},
}


def _write(claude_home: Path, worktree: Path, lines: list[dict]) -> Path:
    proj = claude_home / transcript.encode_project_dir(worktree)
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "sess.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), "utf-8")
    return f


def _home(tmp_path):
    home = tmp_path / "home"
    return home, home / ".claude" / "projects"


def _worktree(home: Path, repo: str, task: str) -> Path:
    return home / "emdash" / "worktrees" / repo / "emdash" / task


def test_a_blocked_session_reports_its_question(tmp_path):
    home, ch = _home(tmp_path)
    _write(ch, _worktree(home, "ace", "spark"), [{"type": "user", "message": {"content": "go"}}, ASK])
    menu = transcript.pending_question_for("ace", "spark", home=home, claude_home=ch)
    assert menu["question"] == "How should the run proceed?"
    assert [o["number"] for o in menu["options"]] == [1, 2]


def test_an_answered_session_reports_nothing(tmp_path):
    home, ch = _home(tmp_path)
    _write(ch, _worktree(home, "ace", "spark"), [ASK, ANSWER])
    assert transcript.pending_question_for("ace", "spark", home=home, claude_home=ch) is None


def test_a_session_with_no_transcript_reports_nothing(tmp_path):
    home, ch = _home(tmp_path)
    ch.mkdir(parents=True)
    assert transcript.pending_question_for("ace", "gone", home=home, claude_home=ch) is None


def test_every_reported_session_is_asked_not_just_the_shown_ones(tmp_path):
    """THE bug this exists to prevent. `attach_recent_tail` caps at
    `session_tail_count` because a tail is only shown for the top few. A blocked
    session is the opposite case: it stops writing the moment it asks, so it
    SINKS in a list ordered by last activity — the longer it waits, the further
    down it goes. Capping this the same way would hide exactly the sessions it
    exists to surface (spark waited 52 minutes)."""
    home, ch = _home(tmp_path)
    sessions = []
    for i in range(40):
        task = f"task-{i}"
        _write(ch, _worktree(home, "ace", task), [ASK] if i == 39 else [{"type": "user", "message": {"content": "hi"}}])
        sessions.append({"project": "ace", "emdash_task": task})

    transcript.attach_pending_questions(sessions, home=home, claude_home=ch)

    # The 40th session — far past any tail cap — still reports its dialog.
    assert sessions[39]["question"]["question"] == "How should the run proceed?"
    assert all(s["question"] is None for s in sessions[:39])


def test_the_field_is_always_present_so_absence_means_no_dialog(tmp_path):
    """`None` is a real answer ("I looked, there is none") and must be sent, not
    omitted. An omitted field would leave a stale menu standing server-side
    after the human answered at the laptop."""
    home, ch = _home(tmp_path)
    _write(ch, _worktree(home, "ace", "quiet"), [{"type": "user", "message": {"content": "hi"}}])
    sessions = [{"project": "ace", "emdash_task": "quiet"}]
    transcript.attach_pending_questions(sessions, home=home, claude_home=ch)
    assert "question" in sessions[0] and sessions[0]["question"] is None


def test_a_broken_transcript_never_costs_the_report(tmp_path):
    """This runs inside the liveness report. Anything that raises here would
    stop the whole fleet's sessions being reported — a far worse outcome than a
    missing menu."""
    home, ch = _home(tmp_path)
    proj = ch / transcript.encode_project_dir(_worktree(home, "ace", "bad"))
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_bytes(b"\xff\xfe not json at all\n{\"type\":")
    sessions = [{"project": "ace", "emdash_task": "bad"},
                {"project": "", "emdash_task": ""},
                {"emdash_task": "no-project"}]
    transcript.attach_pending_questions(sessions, home=home, claude_home=ch)
    assert all(s["question"] is None for s in sessions)


def test_only_the_tail_is_read_for_a_huge_transcript(tmp_path):
    """A blocked agent's question is always the LAST record — it stopped there.
    So the bound only has to cover the ask and its answer, and a session's whole
    history never has to be parsed to find out whether somebody is waiting."""
    home, ch = _home(tmp_path)
    filler = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 4000}]}}] * 200
    _write(ch, _worktree(home, "ace", "big"), filler + [ASK])
    path = transcript.resolve_transcript("ace", "big", home=home, claude_home=ch)
    assert path.stat().st_size > transcript.QUESTION_TAIL_BYTES   # the read is genuinely partial
    menu = transcript.pending_question_for("ace", "big", home=home, claude_home=ch)
    assert menu["question"] == "How should the run proceed?"
