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

from _bundles import bundle

TOKENS = {
    "echo-turn": {"token": "tok-olive", "github_login": "olive", "git_name": "Olive Owner",
                  "git_email": "1+olive@users.noreply.github.com", "requested_by": "Andrea <a@x.org>"},
    "hal-turn": {"token": "tok-nina", "github_login": "nina", "git_name": "Nina",
                 "git_email": "2+nina@users.noreply.github.com", "requested_by": ""},
    "project-turn": {"token": "tok-labs", "github_login": "hal-bot", "git_name": "Hal",
                     "git_email": "3+hal-bot@users.noreply.github.com", "requested_by": ""},
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


def test_a_project_turn_with_no_agent_slug_still_asks_canopy_web_for_a_token(cr):
    """A project-dispatch turn carries `project`, never `agent_slug` — this box
    cannot know whether that project has a configured fallback identity, so it
    must still ask rather than short-circuiting the way a bare repo chat does."""
    env = _env_for(cr, {"id": "project-turn", "agent_slug": "", "project": "canopy-web"})
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "tok-labs"
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Hal"


def test_a_project_turn_whose_project_has_no_default_identity_gets_none(cr, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "shared-pat")
    env = _env_for(cr, {"id": "undefaulted-project-turn", "agent_slug": "", "project": "scout"})
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


def test_a_turn_with_neither_agent_slug_nor_project_never_calls_the_endpoint(cr, monkeypatch):
    """The short-circuit still holds for whatever carries neither — a plain
    session turn with no agent and no project. Calling out for nothing would
    just be a wasted round trip canopy-web can only ever refuse."""
    calls = []
    real_api = cr._api
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (calls.append(a[1]), real_api(*a, **k))[1])
    _env_for(cr, {"id": "session-turn", "agent_slug": "", "project": ""})
    assert not any("github-token" in path for path in calls)


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
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, bundle("c", github_token="old")))
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


# ---- a token a person delegated to the agent lives in its vault -------------------------

def test_a_delegated_token_is_opened_from_the_boxs_own_resolve(cr, monkeypatch):
    """canopy-web hands out no copy of a delegated token, only which vault entry holds it;
    the box takes it from its own resolve (opened with its own key by the canopy CLI)."""
    TOKENS["ace-turn"] = {"token": "", "delegated": "github-token", "agent": "ace", "github_login": "olive",
                          "git_name": "Olive Owner", "git_email": "1+olive@users.noreply.github.com",
                          "repo": "dimagi-internal/ace", "requested_by": ""}
    probed = []
    monkeypatch.setattr(cr, "_probe_delegated_github_soon", lambda *a: probed.append(a))
    monkeypatch.setattr(cr, "_turn_values",
                        lambda slug: cr._AGENT_DELEGATED.update({slug: {"github-token": "tok-vault"}}) or {})
    try:
        env = _env_for(cr, {"id": "ace-turn", "agent_slug": "ace"})
    finally:
        TOKENS.pop("ace-turn")
        cr._AGENT_DELEGATED.clear()
    assert env["GH_TOKEN"] == "tok-vault" and env["GIT_AUTHOR_NAME"] == "Olive Owner"
    assert probed == [("ace", "github-token", "tok-vault", "dimagi-internal/ace")]


def test_a_box_that_cannot_open_it_runs_the_turn_with_no_github_identity(cr, monkeypatch):
    TOKENS["ace-turn"] = {"token": "", "delegated": "github-token", "agent": "ace", "github_login": "olive"}
    monkeypatch.setattr(cr, "_turn_values", lambda slug: {})
    try:
        env = _env_for(cr, {"id": "ace-turn", "agent_slug": "ace"})
    finally:
        TOKENS.pop("ace-turn")
    assert "GH_TOKEN" not in env or not env["GH_TOKEN"]


def test_the_box_probes_a_delegated_token_like_canopy_web_did(cr, monkeypatch):
    calls = []

    def probe(token, method, path, body=None):
        calls.append((method, path))
        if path == "/user":
            return 200, {"github-authentication-token-expiration": "2026-12-01 10:00:00 UTC"}
        return 422, {}

    monkeypatch.setattr(cr, "_github_probe", probe)
    found = cr.probe_github_token("tok", "dimagi-internal/ace")
    assert found["checks"] == [{"repo": "dimagi-internal/ace", "ok": True,
                                "detail": "can push and open pull requests"}]
    assert found["provider_expires_at"].startswith("2026-12-01T10:00:00")
    assert ("POST", "/repos/dimagi-internal/ace/pulls") in calls

    monkeypatch.setattr(cr, "_github_probe", lambda *a, **k: (401, {}))
    assert "rejected" in cr.probe_github_token("tok", "dimagi-internal/ace")["error"]
