"""Claude Code trust for a caller's session: which folder the key must name, which
folders canopy may trust, and what the native layer writes when it may not.

Measured on Claude Code 2.1.332: with `projects[<worktree>].hasTrustDialogAccepted`
alone (what emdash writes), a worktree's `.claude/settings.json` allow rules are dropped
("Dropped 1 project-scoped permissions.allow entry — workspace not yet trusted") and an
interactive start stops on "This folder pre-approves N tool permissions", default "No,
continue without these permissions". With the MAIN checkout's key set, nothing is
dropped and no dialog is shown.
"""
import json
import os
import subprocess

import pytest

from canopy_runner import caller, cdp_control, claude_trust, emdash, execute, stored_agents
from canopy_runner import native_permissions as np


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture()
def repo(tmp_path):
    """A main checkout and one emdash-style worktree of it."""
    main = tmp_path / "agents" / "connect-helper"
    main.mkdir(parents=True)
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    wt = tmp_path / "worktrees" / "emdash-cx-hello-57ydv"
    _git("worktree", "add", "-q", "-b", "cx", str(wt), cwd=main)
    return main.resolve(), wt.resolve()


def _config(tmp_path, projects):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": projects, "oauthAccount": {"k": "v"}}))
    return path


def test_a_worktrees_strict_trust_is_keyed_on_its_main_checkout(repo):
    main, wt = repo
    assert claude_trust.main_checkout(str(wt)) == str(main)
    assert claude_trust.main_checkout(str(main)) == str(main)


def test_not_a_repository_has_no_main_checkout(tmp_path):
    assert claude_trust.main_checkout(str(tmp_path)) is None


def test_the_worktree_key_alone_does_not_make_allow_rules_apply(repo, tmp_path):
    main, wt = repo
    cfg = _config(tmp_path, {str(wt): {"hasTrustDialogAccepted": True}})
    assert claude_trust.allow_rules_apply(str(wt), config=cfg) is False
    cfg = _config(tmp_path, {str(main): {"hasTrustDialogAccepted": True}})
    assert claude_trust.allow_rules_apply(str(wt), config=cfg) is True


def test_a_stored_checkout_is_trusted_and_the_rest_of_the_file_kept(repo, tmp_path):
    main, _ = repo
    cfg = _config(tmp_path, {"/elsewhere": {"hasTrustDialogAccepted": False, "x": 1}})
    assert claude_trust.trust_stored_checkout(str(main), config=cfg, root=main.parent)
    doc = json.loads(cfg.read_text())
    assert doc["projects"][str(main)] == {"hasTrustDialogAccepted": True}
    assert doc["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": False, "x": 1}
    assert doc["oauthAccount"] == {"k": "v"}
    before = cfg.stat().st_mtime_ns
    assert claude_trust.trust_stored_checkout(str(main), config=cfg, root=main.parent)
    assert cfg.stat().st_mtime_ns == before, "already trusted: not rewritten"


def test_canopy_never_trusts_a_folder_outside_its_stored_agents_root(repo, tmp_path):
    main, wt = repo
    cfg = _config(tmp_path, {})
    other_root = tmp_path / "not-agents"
    other_root.mkdir()
    assert not claude_trust.trust_stored_checkout(str(main), config=cfg, root=other_root)
    # A worktree under the root is not a checkout canopy materialized either.
    assert not claude_trust.trust_stored_checkout(str(wt), config=cfg, root=wt.parent)
    assert json.loads(cfg.read_text())["projects"] == {}


def test_a_corrupt_config_is_never_rewritten(repo, tmp_path):
    main, _ = repo
    cfg = tmp_path / "claude.json"
    cfg.write_text("{ not json")
    assert not claude_trust.trust_stored_checkout(str(main), config=cfg, root=main.parent)
    assert cfg.read_text() == "{ not json"


# --- native layer: no allow rules where Claude Code would drop them ----------------

ASK = {"name": "ask", "tools": ["mcp__*canopy-web__who_is_asking", "Read"], "write_paths": []}


def test_an_untrusted_repository_gets_a_deny_only_native_layer(monkeypatch, tmp_path):
    monkeypatch.setattr(np, "mcp_server_names", lambda **k: {"plugin:canopy:canopy-web":
                                                             "plugin_canopy_canopy-web"})
    wt = tmp_path / "wt"
    wt.mkdir()
    np.confine_worktree(ASK, str(wt), allow_rules=False)
    perms = json.loads((wt / ".claude" / "settings.json").read_text())["permissions"]
    assert perms["allow"] == []
    assert perms["deny"], "the deny list needs no trust and is always written"
    np.confine_worktree(ASK, str(wt))
    perms = json.loads((wt / ".claude" / "settings.json").read_text())["permissions"]
    assert perms["allow"], "trusted: the profile's allow rules are written"


# --- wiring: a stored agent's confined create trusts its checkout first -------------

def _stored_confined():
    return {"id": "3f2b8c1e-0000-4000-8000-000000000002", "agent_slug": "connect-helper",
            "definition": {"source": "stored"},
            "origin_ref": {"thread_id": "7"}, "prompt": "hello",
            "caller_context": {"profile": "restricted", "capability": ASK,
                               "conversation": {"thread_id": "7"}}}


def test_a_confined_stored_agent_create_records_trust_before_the_session_exists(
        monkeypatch, tmp_path, repo):
    from tests.test_execute import FakeClient, _cfg

    main, _ = repo
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    monkeypatch.setattr(execute, "NATIVE_WAIT_SECONDS", 0)
    monkeypatch.setattr(execute, "_stored_project", lambda cfg, turn: "")
    monkeypatch.setattr(stored_agents, "agent_dir", lambda slug, home=None: main.parent / slug)
    seen = {}

    def create(agent, prompt, task_name, port):
        seen["trusted"] = claude_trust.is_trusted(str(main))
        return {"task": task_name}

    monkeypatch.setattr(cdp_control, "create_task", create)
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r-1", _stored_confined())
    assert seen == {"trusted": True}


def test_a_full_profile_or_repo_agent_create_trusts_nothing(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(claude_trust, "trust_stored_checkout", lambda p: called.append(p))
    execute._trust_stored_checkout({**_stored_confined(), "caller_context": {"profile": "full"}})
    execute._trust_stored_checkout({**_stored_confined(), "definition": {"source": "repo"}})
    assert called == []
