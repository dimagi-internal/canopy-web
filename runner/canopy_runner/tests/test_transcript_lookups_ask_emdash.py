"""Every per-session transcript lookup asks emdash where the transcript is.

emdash names a worktree for the task it was CREATED as (`emdash-slack-jpxzv`), so a
renamed task (`slack-new`) is not findable by path convention; only emdash's own
`conversations.cwd` knows. The staleness watch, the live stream and backfills
ask. The session tail and the blocked-dialog read must too: otherwise `slack-new`
would report its last activity from emdash's UI clock while it was replying later,
and a question it asked could only reach the phone by the hook.
"""
from canopy_runner import transcript


def _capture(monkeypatch):
    seen = []

    def fake(repo, task, **kw):
        seen.append(kw.get("emdash_db"))
        return None

    monkeypatch.setattr(transcript, "resolve_transcript", fake)
    return seen


def test_the_session_tail_asks_emdash(monkeypatch):
    seen = _capture(monkeypatch)
    transcript.attach_recent_tail([{"project": "p", "emdash_task": "slack-new"}], emdash_db="/db")
    assert seen == ["/db"]


def test_the_blocked_dialog_read_asks_emdash(monkeypatch):
    seen = _capture(monkeypatch)
    transcript.attach_pending_questions([{"project": "p", "emdash_task": "slack-new"}], emdash_db="/db")
    assert seen == ["/db"]
