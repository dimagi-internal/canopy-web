import json
from pathlib import Path

from canopy_transcript import repo_for_cwd, scan_usage


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _assistant(mid, day, out, *, model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": f"{day}T10:00:00Z",
            "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 1000}}}


def test_a_session_in_a_known_repo_is_reported_per_day_and_never_twice(tmp_path):
    home = tmp_path
    projects = home / ".claude" / "projects"
    cwd = str(home / "emdash" / "worktrees" / "canopy-web" / "emdash" / "fix-abcde")
    _write(projects / "a" / "11111111-aaaa.jsonl", [
        {"type": "user", "cwd": cwd, "timestamp": "2026-10-01T09:00:00Z", "message": {"content": "Fix   the login bug"}},
        _assistant("m1", "2026-10-01", 10), _assistant("m1", "2026-10-01", 10),  # one call, two blocks
        _assistant("m2", "2026-10-02", 5),
    ])
    _write(projects / "b" / "22222222-bbbb.jsonl", [
        {"type": "user", "cwd": str(home / "personal" / "taxes"), "message": {"content": "private"}},
        _assistant("m3", "2026-10-01", 99),
    ])
    reports, seen = scan_usage(projects, home=home, repos={"canopy-web", "hal"})
    assert [r["claude_session"] for r in reports] == ["11111111-aaaa"]  # nothing outside known repos
    r = reports[0]
    assert (r["repo"], r["first_at"][:10]) == ("canopy-web", "2026-10-01")
    assert "title" not in r  # no words: not even the first prompt
    assert [(d["day"], d["output_tokens"], d["cache_read_tokens"], d["messages"]) for d in r["days"]] == [
        ("2026-10-01", 10, 1000, 1), ("2026-10-02", 5, 1000, 1)]
    assert scan_usage(projects, home=home, repos={"canopy-web"}, seen=seen)[0] == []  # unchanged: not re-read


def test_repo_for_cwd_reads_agent_checkouts_and_clones(tmp_path):
    repos = {"hal", "canopy-web"}
    assert repo_for_cwd(str(tmp_path / "emdash-projects" / "hal"), home=tmp_path, repos=repos) == "hal"
    assert repo_for_cwd("/opt/agents/hal", home=tmp_path, repos=repos) == "hal"
    assert repo_for_cwd("/code/canopy-web/packages/x", home=tmp_path, repos=repos) == "canopy-web"
    assert repo_for_cwd("/code/other", home=tmp_path, repos=repos) == ""


def test_send_report_remembers_only_what_was_taken(tmp_path):
    from canopy_transcript import send_usage_report

    projects = tmp_path / ".claude" / "projects"
    _write(projects / "a" / "s1.jsonl", [{"type": "user", "cwd": "/x/hal", "message": {"content": "go"}},
                                         _assistant("m1", "2026-10-01", 3)])
    sent = []

    def refuse(chunk):
        raise RuntimeError("down")

    try:
        send_usage_report(refuse, home=tmp_path, repos=["hal"])
    except RuntimeError:
        pass
    assert send_usage_report(lambda c: sent.append(len(c)), home=tmp_path, repos=["hal"]) == 1
    assert send_usage_report(lambda c: sent.append(len(c)), home=tmp_path, repos=["hal"]) == 0
    assert sent == [1]  # a failed post is sent again; a taken one never


def test_a_claude_session_key_names_its_transcript(tmp_path):
    from canopy_transcript import transcript_for_key

    projects = tmp_path / "projects"
    _write(projects / "-x-hal" / "abc-123.jsonl", [{"type": "user"}])
    assert transcript_for_key("claude:abc-123", claude_home=projects) == projects / "-x-hal" / "abc-123.jsonl"
    assert transcript_for_key("emdash-task", claude_home=projects) is None
    assert transcript_for_key("claude:../etc", claude_home=projects) is None
