"""A private conversation's files on a sensitive-data box: fetched for one turn into
its own directory, readable by it alone, gone when it ends.

canopy-web serves each file once, to the box holding the turn, and names it only in
that download (apps/canopy_sessions/private_attachments.py). docs/architecture/
tenancy.md § Sensitive conversations ("Files").
"""
from __future__ import annotations

import json
import os
import pathlib
import stat

CAP = {"name": "data", "entry": None, "tools": ["mcp__*canopy-web__who_is_asking"], "bash": [],
       "read_paths": [], "data": "sensitive"}
TID = "3f2b8c1e-0000-4000-8000-0000000000aa"
CONV = "5e55a0b1-0000-4000-8000-00000000c0de"
NAME = "amina-hiv-results.png"
PNG = b"\x89PNG amina"


def _turn(tid=TID, sensitive=True, attachments=({"id": "a-1", "sealed": True},), cap=CAP):
    return {"id": tid, "agent_slug": "labsdata", "prompt": "what do these say?", "mcp_token": "cct_x",
            "sensitive_conversation": sensitive,
            "origin_ref": {"chat_session_id": CONV, "attachments": list(attachments)},
            "caller_context": {"profile": "confined", "capability": cap, "conversation": {}}}


def _leave(path, text="the host's data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _box(load_cloud_runner, monkeypatch, tmp_path, *, served=None):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    mod = load_cloud_runner()
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS_HOME", tmp_path / "claude" / "projects")
    monkeypatch.setattr(mod, "KEPT_ROOT", tmp_path / "kept")
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    cwd = tmp_path / "work" / "sessions" / CONV
    cwd.mkdir(parents=True)
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: cwd)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    calls = {"api": [], "log": [], "downloads": []}
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: calls["api"].append((m, p, b)) or (200, {}))
    monkeypatch.setattr(mod, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: None)
    monkeypatch.setattr(mod, "_log", lambda msg: calls["log"].append(msg))
    monkeypatch.setattr(mod, "_native_settings", lambda turn, c, caller: str(_leave(
        tmp_path / "profiles" / f"cloud-{turn['id']}.settings.json", "{}")))

    def download(runner_id, turn_id, aid):
        calls["downloads"].append((turn_id, aid))
        if served is None:
            raise OSError("HTTP Error 404: Not Found")
        return served

    monkeypatch.setattr(mod, "_download_attachment", download)
    project = mod.CLAUDE_PROJECTS_HOME / mod._encode_project_dir(cwd)

    def run(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        # What the turn sees while it runs: the file, the prompt, its rules.
        att = mod._sensitive_attachment_dir(cwd, turn_id)
        files = sorted(att.glob("*")) if att.exists() else []
        cmd = mod._claude_cmd(prompt, resume_session_id)
        sid = resume_session_id or cmd[cmd.index("--session-id") + 1]
        settings = json.loads(pathlib.Path(cmd[cmd.index("--settings") + 1]).read_text())
        profile = json.loads((tmp_path / "profiles" / f"cloud-{turn_id}.json").read_text())
        calls["seen"] = {"prompt": prompt, "files": files,
                         "modes": [stat.S_IMODE(f.stat().st_mode) for f in files],
                         "dir_mode": stat.S_IMODE(att.stat().st_mode) if att.exists() else None,
                         "bytes": [f.read_bytes() for f in files],
                         "settings": settings, "profile": profile}
        # The agent read the file: its content is in the session's transcript.
        _leave(project / f"{sid}.jsonl", '{"tool_result": "amina"}\n')
        return True, "ok", sid

    monkeypatch.setattr(mod, "execute_prompt", run)
    return mod, calls, cwd, project


def test_the_file_lands_in_the_turns_own_directory_readable_by_it_alone_and_goes_with_it(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, cwd, project = _box(load_cloud_runner, monkeypatch, tmp_path, served=(PNG, NAME))
    mod._run_turn("r-1", _turn())
    seen = calls["seen"]
    att = mod._sensitive_attachment_dir(cwd, TID)
    [f] = seen["files"]
    assert f == att / f"1-{NAME}" and seen["bytes"] == [PNG]
    assert seen["modes"] == [0o600] and seen["dir_mode"] == 0o700
    assert str(f) in seen["prompt"] and "Read it with the Read tool" in seen["prompt"]
    # Its rules: Read inside that directory (the capability grants no Read at all), and
    # profile_guard's profile says the same.
    rule = mod._abs_read_rule(att, "/**")
    assert rule in seen["settings"]["permissions"]["allow"]
    from canopy_runner import native_permissions as np

    assert not np.denied_by(seen["settings"]["permissions"]["deny"], os.path.realpath(str(f)))
    cap = seen["profile"]["capability"]
    assert "Read" in cap["tools"] and f"{os.path.realpath(str(att))}/**" in cap["read_paths"]
    # Gone when the turn ends; only the kept transcript (the conversation's own session,
    # which lives no longer than the conversation) holds what it read.
    assert not att.exists() and not (cwd / mod.SENSITIVE_ATTACHMENT_DIRNAME).exists()
    kept = json.loads((tmp_path / "kept" / f"{CONV}.json").read_text())
    assert (project / f"{kept['cli_session_id']}.jsonl").exists()
    assert calls["downloads"] == [(TID, "a-1")]
    assert NAME not in " ".join(calls["log"])


def test_another_turns_directory_is_outside_this_turns_reads(load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, cwd, _project = _box(load_cloud_runner, monkeypatch, tmp_path, served=(PNG, NAME))
    mod._run_turn("r-1", _turn())
    allow = calls["seen"]["settings"]["permissions"]["allow"]
    other_conv = tmp_path / "work" / "sessions" / "c-2" / mod.SENSITIVE_ATTACHMENT_DIRNAME / "t-2" / "x.png"
    assert not any(os.path.realpath(str(other_conv)).startswith(a[len("Read(/"):-len("/**)")])
                   for a in allow if a.startswith("Read(/") and a.endswith("/**)"))


def test_a_file_that_cannot_be_fetched_is_named_by_place_and_logged_by_id(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, cwd, _project = _box(load_cloud_runner, monkeypatch, tmp_path, served=None)
    mod._run_turn("r-1", _turn())
    assert "1 attached file could not be downloaded" in calls["seen"]["prompt"]
    assert "attachment 1" in calls["seen"]["prompt"]
    assert any("a-1" in line for line in calls["log"])
    assert NAME not in " ".join(calls["log"])
    # Nothing to read, so no read rule for it.
    att = mod._sensitive_attachment_dir(cwd, TID)
    assert mod._abs_read_rule(att, "/**") not in calls["seen"]["settings"]["permissions"]["allow"]


def test_a_confined_turn_outside_a_private_conversation_fetches_nothing(
        load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, cwd, _project = _box(load_cloud_runner, monkeypatch, tmp_path, served=(PNG, NAME))
    mod._run_turn("r-1", _turn(sensitive=False, attachments=({"id": "a-1", "filename": "x.png"},)))
    assert calls["downloads"] == [] and calls["seen"]["files"] == []
    assert "could not be downloaded" in calls["seen"]["prompt"]


def test_a_turn_that_fails_still_takes_its_files_with_it(load_cloud_runner, monkeypatch, tmp_path):
    mod, calls, cwd, _project = _box(load_cloud_runner, monkeypatch, tmp_path, served=(PNG, NAME))

    there = []

    def boom(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        there.extend(mod._sensitive_attachment_dir(cwd, turn_id).glob("*"))
        raise RuntimeError("the CLI died")

    monkeypatch.setattr(mod, "execute_prompt", boom)
    try:
        mod._run_turn("r-1", _turn())
    except RuntimeError:
        pass
    assert there and not mod._sensitive_attachment_dir(cwd, TID).exists()


def test_the_forget_removes_the_turns_files_and_leaves_another_turns(load_cloud_runner, monkeypatch, tmp_path):
    mod, _calls, cwd, _project = _box(load_cloud_runner, monkeypatch, tmp_path)
    mine = _leave(mod._sensitive_attachment_dir(cwd, TID) / f"1-{NAME}")
    theirs = _leave(mod._sensitive_attachment_dir(cwd, "t-running") / "1-x.png")
    mod._sensitive_data_forget_turn(TID, cwd, [], True)
    assert not mine.parent.exists() and theirs.exists()
