"""A sensitive-data box runs only confined turns.

docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §5.
"""
from __future__ import annotations

CAP = {"name": "data", "entry": None, "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"],
       "data": "sensitive"}
TID = "3f2b8c1e-0000-4000-8000-0000000000aa"


def _turn(**kw):
    d = {"id": TID, "agent_slug": "labsdata", "prompt": "how many visits?", "mcp_token": "cct_x",
         "origin_ref": {}, "caller_context": {"profile": "confined", "capability": CAP,
                                              "conversation": {}}}
    d.update(kw)
    return d


def _wire(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    calls = {"api": []}
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: calls["api"].append((m, p, b)) or (200, {}))
    monkeypatch.setattr(mod, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: None)

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        calls["exec"] = prompt
        return True, "ok", "cli-1"

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    return calls


def _finish(calls):
    return [b for _m, p, b in calls["api"] if p.endswith("/finish")]


def test_a_sensitive_box_refuses_an_unconfined_turn(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert "exec" not in calls
    [f] = _finish(calls)
    assert f["status"] == "failed" and f["result_note"].startswith("sensitive_data_unconfined")


def test_a_confined_turn_ships_its_transcript_before_it_is_deleted(
        load_cloud_runner, monkeypatch, tmp_path):
    """The rows carry the reply to the asker (canopy-web seals them); deleting the
    transcript first left the visitor with no reply."""
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: order.append("ship"))
    monkeypatch.setattr(mod, "_sensitive_data_forget_turn", lambda *a, **k: order.append("forget"))
    mod._run_turn("r-1", _turn(chat_session_id="5e55-1"))
    assert calls.get("exec"), _finish(calls)
    assert order == ["ship", "forget"]
