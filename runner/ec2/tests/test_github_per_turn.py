"""GitHub on the cloud runner is per TURN, never per box (#747).

The box used to export one shared token as GH_TOKEN into its own environment
(so every turn inherited it) and write a global ~/.git-credentials. Now each
turn asks canopy-web for ITS agent owner's token and it lives in that turn's
environment alone. What these pin:

- two concurrent turns for agents with different owners each see only their own;
- nothing box-wide leaks in — not this process's GH_TOKEN, not an agent's .env;
- a turn canopy refuses runs with NO GitHub identity rather than a leftover one;
- commits carry the owner's identity instead of "Ubuntu <ubuntu@ip-…>";
- the shared-PAT era's files are removed at start.
"""
from __future__ import annotations

import subprocess
import threading

import pytest

TOKENS = {
    "echo-turn": {"token": "tok-olive", "github_login": "olive", "git_name": "Olive Owner",
                  "git_email": "1+olive@users.noreply.github.com", "requested_by": "Andrea <a@x.org>"},
    "hal-turn": {"token": "tok-nina", "github_login": "nina", "git_name": "Nina",
                 "git_email": "2+nina@users.noreply.github.com", "requested_by": ""},
}


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_agent_op_token", lambda slug: "")

    def api(method, path, body=None, **_):
        if path.endswith("/github-token"):
            turn_id = path.split("/turns/", 1)[1].split("/", 1)[0]
            return (200, TOKENS[turn_id]) if turn_id in TOKENS else (409, None)
        return 200, {}

    monkeypatch.setattr(cloud_runner, "_api", api)
    return cloud_runner


def _env_for(cr, turn):
    """The env a turn's subprocess gets, computed on its own thread the way
    `_run_turn` does it."""
    cr._TURN_ENV.extra = cr._github_turn_env("r1", turn)
    try:
        return cr._agent_env(turn.get("agent_slug"))
    finally:
        cr._TURN_ENV.extra = {}


def test_a_turn_gets_its_owners_token_and_git_identity(cr):
    env = _env_for(cr, {"id": "echo-turn", "agent_slug": "echo"})
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "tok-olive"
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Olive Owner"
    assert env["GIT_AUTHOR_EMAIL"] == "1+olive@users.noreply.github.com"
    assert env["CANOPY_REQUESTED_BY"] == "Andrea <a@x.org>"


def test_concurrent_turns_for_different_owners_do_not_see_each_other(cr):
    seen: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def run(turn):
        cr._TURN_ENV.extra = cr._github_turn_env("r1", turn)
        barrier.wait()  # both turns hold their env at the same moment
        seen[turn["id"]] = cr._agent_env(turn["agent_slug"])
        cr._TURN_ENV.extra = {}

    threads = [threading.Thread(target=run, args=({"id": t, "agent_slug": s},))
               for t, s in (("echo-turn", "echo"), ("hal-turn", "hal"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["echo-turn"]["GH_TOKEN"] == "tok-olive"
    assert seen["hal-turn"]["GH_TOKEN"] == "tok-nina"
    assert seen["hal-turn"]["GIT_AUTHOR_NAME"] == "Nina"


def test_nothing_box_wide_or_from_the_agents_env_leaks_in(cr, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "shared-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-pat")
    (tmp_path / ".echo").mkdir()
    (tmp_path / ".echo" / ".env").write_text("GITHUB_TOKEN=from-dotenv\nOTHER=kept\n")

    env = _env_for(cr, {"id": "echo-turn", "agent_slug": "echo"})
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "tok-olive"
    assert env["OTHER"] == "kept"


def test_a_refused_turn_runs_with_no_github_identity_at_all(cr, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "shared-pat")
    env = _env_for(cr, {"id": "no-delegation", "agent_slug": "ada"})
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert "GIT_CONFIG_COUNT" not in env


def test_the_git_helper_answers_with_the_turns_token(cr, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           **cr.github_env("tok-olive", git_name="O", git_email="o@x")}
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert "username=x-access-token" in out.stdout
    assert "password=tok-olive" in out.stdout


def test_the_shared_pat_eras_files_are_removed_at_start(cr, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "shared-pat")
    creds = tmp_path / ".git-credentials"
    creds.write_text("https://x-access-token:shared-pat@github.com\n")
    subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, check=True)
    # HOME is the temp dir (fixture), so the runner's `git config --global` edits it.
    cr._retire_shared_github_credentials()

    assert not creds.exists()
    assert "GH_TOKEN" not in cr.os.environ
    helper = subprocess.run(["git", "config", "--global", "--get", "credential.helper"],
                            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True)
    assert helper.stdout.strip() == ""


def test_the_credential_bundle_no_longer_stages_github(cr, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {"claude_token": "c", "github_token": "old"}))
    monkeypatch.setattr(cr, "_apply_claude_credential", lambda i: None)
    assert cr.fetch_and_stage_credential("r1") is True
    assert "GH_TOKEN" not in cr.os.environ


def test_a_chat_turn_carries_its_chats_key_and_leaves_it_for_the_mcp_helper(cr, tmp_path, monkeypatch):
    """The key goes into this turn's env for `canopy secret`, and into a 0600
    file the MCP headers helper can find — Claude Code strips secret-looking
    variables from the helper's environment, so the env alone cannot reach it."""
    import stat

    monkeypatch.setattr(cr, "CHAT_KEY_ROOT", tmp_path / "chat")
    chat = "eb742bd8-0000-4000-8000-00000000000a"
    turn = {"id": "echo-turn", "agent_slug": "echo", "chat_key": "chk_abc",
            "origin_ref": {"chat_session_id": chat}}
    cr._TURN_ENV.extra = {**cr._github_turn_env("r1", turn), **cr._chat_key_env(turn)}
    try:
        env = cr._agent_env("echo")
    finally:
        cr._TURN_ENV.extra = {}
    assert env["CANOPY_CHAT_KEY"] == "chk_abc" and env["CANOPY_CHAT_SESSION"] == chat
    f = tmp_path / "chat" / "chat" / f"{chat}.key"
    assert f.read_text() == "chk_abc"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600

    # A turn that is not a chat's carries no key.
    assert cr._chat_key_env({"id": "t", "agent_slug": "echo"}) == {}
