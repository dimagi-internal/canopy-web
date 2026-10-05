"""Every per-session transcript lookup asks emdash where the transcript is.

emdash names a worktree for the task it was CREATED as (`emdash-slack-jpxzv`), so a
renamed task (`slack-new`) is not findable by path convention; only emdash's own
`conversations.cwd` knows. The staleness watch, the live stream and backfills
already asked. The session tail and the blocked-dialog read did not, so on
2026-10-05 `slack-new` reported its last activity as 12:04 (emdash's UI clock)
while it was replying at 17:14, and a question it asked could only have reached
the phone by the hook.
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
