"""A caller's turn on the cloud runner runs confined — CANOPY_PROFILE points the
session at its profile, it never resumes or becomes a resume target, and the box
only claims such turns when its canopy guard honours the variable."""
from __future__ import annotations

import json
import os
import stat

import pytest

CAP = {"name": "ask", "entry": "/ace:ask --thread {thread_id}", "tools": ["Read"],
       "bash": [], "read_paths": ["{cwd}/**"]}
TID = "3f2b8c1e-0000-4000-8000-000000000002"


def _turn(**kw):
    d = {"id": TID, "agent_slug": "ace", "prompt": "/ace:turn --thread 18c9", "mcp_token": "cct_x",
         "origin_ref": {"thread_id": "18c9"},
         "caller_context": {"profile": "restricted", "capability": CAP,
                            "conversation": {"thread_id": "18c9"}}}
    d.update(kw)
    return d


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(cloud_runner, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(cloud_runner, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(cloud_runner, "_start_lease_renewal",
                        lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(cloud_runner, "_child_safe_env", lambda: {"PATH": "/bin"})
    calls = {"api": [], "recorded": [], "shipped": []}
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, b=None, **k: calls["api"].append((m, p, b)) or (200, {}))
    monkeypatch.setattr(cloud_runner, "_record_session_resume",
                        lambda rid, turn, sid: calls["recorded"].append(sid))
    monkeypatch.setattr(cloud_runner, "_ship_transcript_rows",
                        lambda rid, turn, cwd, sid: calls["shipped"].append(sid))

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        env = cloud_runner._agent_env(agent_slug)
        calls["exec"] = {"prompt": prompt, "resume": resume_session_id,
                         "profile": env.get("CANOPY_PROFILE")}
        if env.get("CANOPY_PROFILE"):
            calls["profile_doc"] = json.loads(open(env["CANOPY_PROFILE"]).read())
            calls["mode"] = stat.S_IMODE(os.stat(env["CANOPY_PROFILE"]).st_mode)
        return True, "PONG", "cli-session-1"

    monkeypatch.setattr(cloud_runner, "execute_prompt", fake_execute)
    return cloud_runner, calls


def test_a_confined_turn_runs_with_its_profile_and_entry(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn(_resume_id="old-session"))
    ex = calls["exec"]
    assert ex["prompt"].startswith("/ace:ask --thread 18c9 --caller ")
    caller_path = ex["prompt"].split("--caller ", 1)[1]
    assert json.loads(open(caller_path).read())["profile"] == "restricted"
    assert calls["profile_doc"]["caller_path"] == caller_path
    assert ex["resume"] is None                              # never resumes
    assert ex["profile"].endswith(f"cloud-{TID}.json")
    assert calls["profile_doc"]["capability"]["name"] == "ask"
    assert calls["profile_doc"]["mcp_token"] == "cct_x"
    assert calls["mode"] == 0o600


def test_it_never_becomes_a_resume_target_but_its_transcript_ships(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn())
    assert calls["recorded"] == [] and calls["shipped"] == ["cli-session-1"]


def test_the_agents_own_env_cannot_switch_the_profile_off(cr, tmp_path, monkeypatch):
    mod, calls = cr
    monkeypatch.setattr(mod.pathlib.Path, "home", lambda: tmp_path)
    (tmp_path / ".ace").mkdir()
    (tmp_path / ".ace" / ".env").write_text("CANOPY_PROFILE=/dev/null\n")
    mod._run_turn("r-1", _turn())
    assert calls["exec"]["profile"].endswith(f"cloud-{TID}.json")


def test_the_profile_env_does_not_leak_into_the_next_turn(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn())
    full = _turn(caller_context={"profile": "full", "capability": None}, _resume_id="s1")
    mod._run_turn("r-1", full)
    assert calls["exec"]["profile"] is None and calls["exec"]["resume"] == "s1"
    assert calls["exec"]["prompt"] == "/ace:turn --thread 18c9"


def test_an_entry_needing_a_thread_fails_the_turn_instead_of_running_it(cr):
    mod, calls = cr
    t = _turn(origin_ref={}, caller_context={"profile": "restricted", "capability": CAP,
                                             "conversation": {}})
    mod._run_turn("r-1", t)
    assert "exec" not in calls
    finish = [b for m, p, b in calls["api"] if p.endswith("/finish")]
    assert finish and finish[0]["status"] == "failed" and "not run" in finish[0]["result_note"]


def test_a_chat_turn_with_no_thread_runs_the_persons_own_words_confined(cr):
    # A Slack or web-chat caller has no thread; a thread-bound entry is for email.
    # Refusing broke every confined chat turn here (found building Hal's `ask`).
    mod, calls = cr
    t = _turn(prompt="how does routing work?",
              origin_ref={"chat_session_id": "s-1"},
              caller_context={"profile": "restricted", "capability": CAP, "conversation": {}})
    mod._run_turn("r-1", t)
    assert calls["exec"]["prompt"] == "how does routing work?"
    assert calls["exec"]["profile"]            # still confined


def _plugin(tmp_path, version, registered=True):
    root = tmp_path / "canopy"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "profile_guard.py").write_text(f"PROFILE_ENFORCEMENT_VERSION = {version}\n")
    (root / "hooks" / "hooks.json").write_text('{"x": "profile_guard.py"}' if registered else "{}")
    return root


def test_supported_only_with_a_guard_that_honours_the_env(cloud_runner, tmp_path):
    assert cloud_runner.profiles_supported(_plugin(tmp_path / "a", 2)) == 2
    assert cloud_runner.profiles_supported(_plugin(tmp_path / "b", 1)) == 0      # laptop-only guard
    assert cloud_runner.profiles_supported(_plugin(tmp_path / "c", 2, registered=False)) == 0
    assert cloud_runner.profiles_supported(tmp_path / "nothing") == 0


def test_every_heartbeat_says_so(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "profiles_supported", lambda: 2)
    monkeypatch.setattr(cloud_runner, "health_report", lambda: {})
    assert cloud_runner._heartbeat_body([])["profiles"] == 2


def test_no_host_credential_ever_rides_the_profile(tmp_path, monkeypatch):
    """A visitor's host credential never reaches a runner (host grant contract
    v1): canopy-web's gateway attaches it server-side. Even if a claim response
    carried something extra, the profile writes only what it names."""
    import runner.ec2.cloud_runner as cr

    monkeypatch.setattr(cr, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(cr, "CALLER_ROOT", tmp_path / "caller")
    turn = {"id": TID, "agent_slug": "ace", "prompt": "/ace:turn --thread 18c9",
            "mcp_token": "cct_x", "capability": {"name": "ask", "entry": "/ace:turn"},
            "on_behalf_of": {"assertion": "eyJ…"}}
    cr._confine(turn)
    doc = json.loads((tmp_path / "profiles" / f"cloud-{TID}.json").read_text())
    assert "on_behalf_of" not in doc
    assert "eyJ" not in json.dumps(doc)


# --- who is asking, for every turn; the native layer, for a caller's ------------------

@pytest.fixture()
def with_canopy_runner(monkeypatch):
    """The box exposes runner/canopy_runner on sys.path at boot (`_expose_repo_package`)."""
    import pathlib
    import sys
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).resolve().parents[2] / "canopy_runner"))
    sys.modules.pop("canopy_runner.native_permissions", None)


def test_every_turn_points_the_prompt_hook_at_its_envelope(cr, monkeypatch):
    mod, calls = cr
    seen = {}

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        seen["caller"] = mod._agent_env(agent_slug).get("CANOPY_CALLER")
        return True, "ok", ""

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    full = _turn(prompt="please deploy", caller_context={"profile": "full", "relationship": "caller",
                                                         "verified": False})
    mod._run_turn("r-1", full)
    assert seen["caller"].endswith(f"{TID}.json")
    assert json.loads(open(seen["caller"]).read())["relationship"] == "caller"


def test_no_envelope_no_pointer(cr, monkeypatch):
    mod, calls = cr
    seen = {}
    monkeypatch.setattr(mod, "execute_prompt", lambda p, t, e, cwd=None, agent_slug=None,
                        resume_session_id=None: seen.update(
                            c=mod._agent_env(agent_slug).get("CANOPY_CALLER")) or (True, "", ""))
    mod._run_turn("r-1", _turn(caller_context=None))
    assert seen["c"] is None


def test_a_confined_turn_passes_its_native_deny_rules_with_settings(cr, monkeypatch, with_canopy_runner):
    mod, calls = cr
    seen = {}

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        seen["cmd"] = mod._claude_cmd(prompt)
        return True, "ok", ""

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    mod._run_turn("r-1", _turn())
    cmd = seen["cmd"]
    assert "--dangerously-skip-permissions" in cmd and "--settings" in cmd
    doc = json.loads(open(cmd[cmd.index("--settings") + 1]).read())
    deny = doc["permissions"]["deny"]
    assert "Bash" in deny and "Edit" in deny and "mcp__*" in deny   # CAP grants neither
    assert mod._TURN_ENV.settings is None                            # and it does not leak


def test_a_full_turn_gets_no_settings(cr, monkeypatch, with_canopy_runner):
    mod, calls = cr
    seen = {}
    monkeypatch.setattr(mod, "execute_prompt", lambda p, t, e, cwd=None, agent_slug=None,
                        resume_session_id=None: seen.update(cmd=mod._claude_cmd(p)) or (True, "", ""))
    mod._run_turn("r-1", _turn(caller_context={"profile": "full"}))
    assert "--settings" not in seen["cmd"]


def test_without_the_module_the_turn_still_runs_confined_by_the_hook(cr, monkeypatch):
    mod, calls = cr
    import builtins
    real = builtins.__import__

    def no_runner(name, *a, **k):
        if name.startswith("canopy_runner"):
            raise ImportError("not on this box")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_runner)
    mod._run_turn("r-1", _turn())
    assert calls["exec"]["profile"].endswith(f"cloud-{TID}.json")
