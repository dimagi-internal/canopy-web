"""A caller's (confined) turn holds no credential it does not need.

profile_guard used to be the only thing between a prompt-injected caller session
and everything the box held: the runner owner's canopy PAT (CANOPY_TOKEN), the
agent's 1Password key, its whole ~/.<slug>/.env (CANOPY_WEB_PAT among it), the
owner's GitHub token and the chat's key. A confined turn's env is now an
allowlist — Claude's own login, locale/paths/TLS, which agent and turn this is —
plus gog's keyring password only for a capability whose bash allowlist runs the
mail path. A full-profile turn is unchanged.
"""
from __future__ import annotations

import pytest

TID = "3f2b8c1e-0000-4000-8000-000000000009"
ASK = {"name": "ask", "entry": "/ace:ask --thread {thread_id}", "tools": ["Read"],
       "bash": ["canopy email read --repo . {thread_id}",
                "bin/ace-email --reply-all --thread-id {thread_id} --body-file {cwd}/.ace-ask/*"],
       "read_paths": ["{cwd}/**"], "write_paths": ["{cwd}/.ace-ask/*"]}
CONNECT = {"name": "connect", "entry": None, "tools": ["Read", "mcp__*canopy-web__current_page"],
           "bash": [], "read_paths": ["{cwd}/**"], "write_paths": []}

BOX_ENV = {
    "PATH": "/usr/bin", "HOME": "/home/ubuntu", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    "CANOPY_BASE_URL": "https://canopy.example",
    "CANOPY_TOKEN": "cpat_runner_owner",                 # the runner owner's PAT
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-login",           # Claude itself must run
    "GOG_KEYRING_PASSWORD": "gog-pw",
    "OP_SERVICE_ACCOUNT_TOKEN": "ops_box",
    "GH_TOKEN": "box-gh", "AWS_SECRET_ACCESS_KEY": "aws",
}
SECRETS = ("CANOPY_TOKEN", "CANOPY_WEB_PAT", "OP_SERVICE_ACCOUNT_TOKEN", "GH_TOKEN",
           "GITHUB_TOKEN", "GIT_CONFIG_VALUE_1", "CANOPY_CHAT_KEY", "AWS_SECRET_ACCESS_KEY",
           "AGENT_SECRET")


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    mod = cloud_runner
    monkeypatch.setattr(mod.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(mod, "CHAT_KEY_ROOT", tmp_path / "chat")
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_native_settings", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_repo_worktree_env", lambda turn, tid: {})
    monkeypatch.setattr(mod, "_start_lease_renewal",
                        lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: dict(BOX_ENV))
    monkeypatch.setattr(mod, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: None)
    (tmp_path / ".ace").mkdir()
    (tmp_path / ".ace" / ".env").write_text("CANOPY_WEB_PAT=cpat_agent\nAGENT_SECRET=s3cret\n")
    calls = {"api": []}

    def api(method, path, body=None, **_):
        calls["api"].append(path)
        if path.endswith("/github-token"):
            return 200, {"token": "owner-gh", "github_login": "olive"}
        if path.endswith("/credentials/resolve"):
            return 200, {"op_sa_token": "ops_agent"}
        return 200, {}

    monkeypatch.setattr(mod, "_api", api)

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        calls["env"] = mod._agent_env(agent_slug)
        return True, "ok", ""

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    return mod, calls


def _turn(cap, **kw):
    d = {"id": TID, "agent_slug": "ace", "prompt": "hi", "mcp_token": "cct_x",
         "chat_key": "ck_chat", "origin_ref": {"thread_id": "18c9", "chat_session_id":
                                               "11111111-2222-4333-8444-555555555555"},
         "caller_context": {"profile": "confined", "capability": cap,
                            "conversation": {"thread_id": "18c9"}}}
    d.update(kw)
    return d


def test_a_confined_turn_holds_none_of_the_secrets(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn(CONNECT))
    env = calls["env"]
    for key in SECRETS:
        assert key not in env, key
    assert not any("cpat_" in v or v in ("owner-gh", "ops_agent", "ck_chat") for v in env.values())
    # …and it never ASKED for the owner's GitHub token or the agent's credentials.
    assert not any(p.endswith("/github-token") or p.endswith("/credentials/resolve")
                   for p in calls["api"])


def test_what_a_confined_turn_keeps_is_what_it_needs_to_run(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn(CONNECT))
    env = calls["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-login"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/ubuntu"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["CANOPY_AGENT"] == "ace"
    assert env["CANOPY_PROFILE"].endswith(f"cloud-{TID}.json")   # → its caller token
    assert env["CANOPY_CALLER"].endswith(f"{TID}.json")
    assert env["CANOPY_TURN_ID"] == TID
    assert "GOG_KEYRING_PASSWORD" not in env                     # no mail in `connect`


def test_only_a_capability_that_sends_mail_gets_the_mail_keyring(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn(ASK))
    env = calls["env"]
    assert env["GOG_KEYRING_PASSWORD"] == "gog-pw"
    for key in SECRETS:
        assert key not in env, key


@pytest.mark.parametrize("rule, mail", [
    ("canopy email read --repo . {thread_id}", True),
    ("bin/hal-email --reply-all --thread-id {thread_id}", True),
    ("./bin/ace-email --reply-all", True),
    ("canopy emailx read", False),
    ("bin/ace-emailer --x", False),
    ("echo canopy email read", False),
])
def test_mail_is_derived_from_the_capabilitys_own_bash_rules(cloud_runner, rule, mail):
    assert cloud_runner._capability_uses_mail({"bash": [rule]}) is mail


def test_a_full_profile_turn_is_unchanged(cr):
    mod, calls = cr
    full = _turn(None, caller_context={"profile": "full"})
    mod._run_turn("r-1", full)
    env = calls["env"]
    assert env["GH_TOKEN"] == "owner-gh"                      # the owner's, per turn
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_agent"     # the agent's own key
    assert env["CANOPY_WEB_PAT"] == "cpat_agent"              # the agent's .env
    assert env["CANOPY_TOKEN"] == "cpat_runner_owner"
    assert env["CANOPY_CHAT_KEY"] == "ck_chat"
    assert "CANOPY_PROFILE" not in env


def test_the_scrub_does_not_outlive_the_confined_turn(cr):
    mod, calls = cr
    mod._run_turn("r-1", _turn(CONNECT))
    mod._run_turn("r-1", _turn(None, caller_context={"profile": "full"}))
    assert calls["env"]["GH_TOKEN"] == "owner-gh"
    assert mod._TURN_ENV.confined is None
