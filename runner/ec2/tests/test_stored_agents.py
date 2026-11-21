"""A STORED agent's turns on a cloud box: materialized by `canopy agent prepare`, kept
current by `canopy agent sync` in place of `git pull`, never given a GitHub identity, and
never run without the materialized clone or past a CLI too old to sync it.

The canopy CLI is faked throughout: `_LAZY_BOOTSTRAPPER` (prepare) and `_STORED_SYNCER`
(sync) stand in for it, writing what the real one writes — a git repo whose HEAD is
`refs/remotes/origin/main`, one orphan commit per revision, origin `canopy://<ws>/<slug>`.
`stored_sync` itself is driven through a fake `subprocess.run`.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import types

import pytest

SLUG = "connect-helper"
STORED = {"source": "stored", "revision": 1, "root_sha256": "ab" * 32}


def _git(*args, cwd, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def materialize(dest: pathlib.Path, revision: int, files: dict[str, str]) -> str:
    """What `canopy agent materialize` leaves: tracked files replaced, ignored ones kept,
    ONE orphan commit, main == origin/main checked out, origin `canopy://connect/<slug>`."""
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _git("init", "-q", "-b", "main", cwd=dest)
    for tracked in _git("ls-files", cwd=dest).splitlines():
        if tracked not in files:
            (dest / tracked).unlink()
    for path, text in files.items():
        (dest / path).parent.mkdir(parents=True, exist_ok=True)
        (dest / path).write_text(text)
    _git("add", "-A", cwd=dest)
    tree = _git("write-tree", cwd=dest)
    env = {"GIT_AUTHOR_NAME": "canopy", "GIT_AUTHOR_EMAIL": "agents@canopy.dimagi.com",
           "GIT_COMMITTER_NAME": "canopy", "GIT_COMMITTER_EMAIL": "agents@canopy.dimagi.com",
           "GIT_AUTHOR_DATE": "2026-11-21T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-21T00:00:00Z",
           "PATH": "/usr/bin:/bin"}
    commit = _git("commit-tree", tree, "-m", f"canopy revision {revision}", cwd=dest, env=env)
    _git("update-ref", "refs/heads/main", commit, cwd=dest)
    _git("update-ref", "refs/remotes/origin/main", commit, cwd=dest)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=dest)
    _git("reset", "-q", "--hard", "main", cwd=dest)
    if _git("remote", cwd=dest):
        _git("remote", "set-url", "origin", f"canopy://connect/{dest.name}", cwd=dest)
    else:
        _git("remote", "add", "origin", f"canopy://connect/{dest.name}", cwd=dest)
    return commit


REV1 = {"CLAUDE.md": "You are the Connect helper.\n", ".gitignore": ".env\n",
        "skills/worker-chat/SKILL.md": "---\nname: worker-chat\n---\nv1\n"}
REV2 = {**REV1, "skills/worker-chat/SKILL.md": "---\nname: worker-chat\n---\nv2\n"}


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    mod = cloud_runner
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mod, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(mod, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    monkeypatch.setattr(mod, "_LAZY_BOOTSTRAP_TRIED", {})
    monkeypatch.setattr(mod, "_STORED_AGENTS", set())
    calls = types.SimpleNamespace(prepare=[], clone=[], sync=[], live=REV1, revision=1)

    def prepare(slug, env=None):
        calls.prepare.append((slug, dict(env or {})))
        materialize(agents / slug, calls.revision, calls.live)

    def sync(slug):
        calls.sync.append(slug)
        before = _git("rev-parse", "HEAD", cwd=agents / slug)
        commit = materialize(agents / slug, calls.revision, calls.live)
        return {"slug": slug, "revision": calls.revision, "root_sha256": "x", "commit": commit,
                "changed": commit != before}

    monkeypatch.setattr(mod, "_LAZY_BOOTSTRAPPER", prepare)
    monkeypatch.setattr(mod, "_LAZY_CLONER", lambda slug, env: calls.clone.append(slug))
    monkeypatch.setattr(mod, "_STORED_SYNCER", sync)
    return mod, agents, calls


def _turn(**kw):
    return {"id": "aaaaaaaa-0000", "agent_slug": SLUG, "origin_ref": {}, "definition": STORED, **kw}


# ── first turn: materialized, never cloned ─────────────────────────────────────

def test_a_first_turn_materializes_the_agent_and_runs_in_a_worktree_of_it(box):
    mod, agents, calls = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert [s for s, _ in calls.prepare] == [SLUG]
    assert calls.clone == []                       # no GitHub clone is attempted
    assert wt.parent == pathlib.Path(mod.WORK_DIR) / "agents" / SLUG
    assert (wt / "CLAUDE.md").read_text() == REV1["CLAUDE.md"]
    assert mod._is_stored_clone(agents / SLUG)
    assert mod.agents_unprepared() == []           # `.git` is there: prepared


def test_prepare_is_handed_no_github_identity(box):
    mod, _, calls = box
    mod._turn_cwd(_turn(), "aaaaaaaa-1", env={"GH_TOKEN": "x", "CANOPY_REQUESTED_BY": "a"})
    assert "GH_TOKEN" not in calls.prepare[0][1]


# ── later turns: sync in place of pull ────────────────────────────────────────

def test_a_later_turn_syncs_and_sees_the_new_revision(box):
    mod, agents, calls = box
    materialize(agents / SLUG, 1, REV1)
    (agents / SLUG / ".env").write_text("X=1\n")
    calls.live, calls.revision = REV2, 2
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert calls.prepare == [] and calls.sync == [SLUG]
    assert (wt / "skills/worker-chat/SKILL.md").read_text().endswith("v2\n")
    assert (wt / ".env").is_symlink()              # ignored state still linked in


def test_a_chat_session_on_a_stored_agent_is_cut_from_the_synced_revision(box):
    mod, agents, calls = box
    materialize(agents / SLUG, 1, REV1)
    chat = _turn(origin_ref={"chat_session_id": "sess-1"})
    first = mod._turn_cwd(chat, "aaaaaaaa-1", env={})
    assert first == pathlib.Path(mod.WORK_DIR) / "sessions" / "sess-1"
    calls.live, calls.revision = REV2, 2
    again = mod._turn_cwd(chat, "aaaaaaaa-2", env={})
    assert again == first
    assert (again / "skills/worker-chat/SKILL.md").read_text().endswith("v2\n")


def test_a_confined_turn_on_a_stored_agent_runs_and_borrows_no_token(box, monkeypatch):
    mod, agents, calls = box
    materialize(agents / SLUG, 1, REV1)
    monkeypatch.setattr(mod, "_AGENT_DELEGATED", {SLUG: {"github-token": "ghp_x"}})
    assert mod._clone_refresh_env(SLUG, {"A": "1"}) == {"A": "1"}
    chat = _turn(origin_ref={"chat_session_id": "sess-9"},
                 caller_context={"profile": "read", "capability": {"name": "read"}})
    wt = mod._turn_cwd(chat, "aaaaaaaa-1", env={"A": "1"})
    assert (wt / "CLAUDE.md").exists() and calls.sync == [SLUG]


def test_git_never_fetches_a_canopy_origin(box, monkeypatch):
    mod, agents, calls = box
    materialize(agents / SLUG, 1, REV1)
    real = subprocess.run
    seen = []

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            seen.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", spy)
    assert mod._fetch_clone(agents / SLUG) is True
    mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert not [c for c in seen if "fetch" in c or "pull" in c]


# ── never run stale code or no code ───────────────────────────────────────────

def test_a_cli_too_old_to_sync_fails_the_turn_with_the_update_hint(box, monkeypatch):
    mod, agents, _ = box
    materialize(agents / SLUG, 1, REV1)
    monkeypatch.setattr(mod, "_STORED_SYNCER", mod.stored_sync)
    monkeypatch.setattr(mod, "_bearer", lambda **_: "runner-token")
    real = subprocess.run

    def run(cmd, *a, **k):
        if cmd[0] != "git":  # the canopy CLI, too old for `sync --runner`
            return subprocess.CompletedProcess(
                cmd, 2, "", "Usage: canopy agent sync [OPTIONS]\nError: No such option '--runner'.\n")
        return real(cmd, *a, **k)

    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.StoredAgentError, match="update the canopy plugin"):
        mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert "--runner" in mod._CLONE_FETCH[str(agents / SLUG)]["error"]


def test_a_failed_sync_runs_the_last_revision_and_says_so(box, monkeypatch):
    mod, agents, _ = box
    materialize(agents / SLUG, 1, REV1)

    def broken(slug):
        raise mod.StoredAgentError("`canopy agent sync` for stored agent connect-helper failed: 503")

    monkeypatch.setattr(mod, "_STORED_SYNCER", broken)
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert (wt / "CLAUDE.md").exists()
    rec = mod._CLONE_FETCH[str(agents / SLUG)]
    rec["ok_at"] = 0.0
    monkeypatch.setattr(mod, "_clone_age", lambda clone, now: 10 ** 6)
    mod._clones_check()
    check = mod._HEALTH["clones"]
    assert check["status"] == "warn" and "STALE revision of a stored agent" in check["detail"]
    assert "GitHub" not in check["detail"]


def test_no_materialized_clone_fails_the_turn_rather_than_run_in_a_scratch_dir(box, monkeypatch):
    mod, _, calls = box
    monkeypatch.setattr(mod, "_LAZY_BOOTSTRAPPER", lambda slug, env=None: calls.prepare.append(slug))
    with pytest.raises(mod.StoredAgentError, match="has not materialized it"):
        mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})


def test_an_agent_converted_to_stored_is_materialized_over_its_github_clone(box, tmp_path):
    mod, agents, calls = box
    origin = tmp_path / "gh"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CLAUDE.md").write_text("old\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v0", cwd=origin)
    _git("clone", "-q", str(origin), str(agents / SLUG), cwd=tmp_path)
    assert not mod._is_stored_clone(agents / SLUG)
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1", env={})
    assert [s for s, _ in calls.prepare] == [SLUG]
    assert (wt / "CLAUDE.md").read_text() == REV1["CLAUDE.md"]


# ── no GitHub identity ────────────────────────────────────────────────────────

def test_a_stored_agents_turn_asks_canopy_web_for_no_github_identity(box, monkeypatch):
    mod, _, _ = box
    asked = []
    monkeypatch.setattr(mod, "_api", lambda *a, **k: asked.append(a) or (200, {"token": "t"}))
    env = mod._github_turn_env("r1", {**_turn(), "id": "t-1"})
    assert asked == []
    assert "GH_TOKEN" not in env and mod.AGENT_REPO_ENV not in env
    assert SLUG in mod._STORED_AGENTS


def test_a_repo_agents_turn_still_does(box, monkeypatch):
    mod, _, _ = box
    asked = []
    monkeypatch.setattr(mod, "_api", lambda *a, **k: asked.append(a) or (409, {}))
    mod._github_turn_env("r1", {"id": "t-1", "agent_slug": "hal", "origin_ref": {}})
    assert asked and asked[0][1].endswith("/github-token")


# ── the CLI call itself ───────────────────────────────────────────────────────

def test_stored_sync_runs_the_runner_sync_as_the_box_and_reads_its_json_line(cloud_runner, monkeypatch):
    mod = cloud_runner
    monkeypatch.setattr(mod, "_bearer", lambda **_: "runner-token")
    monkeypatch.setenv("GH_TOKEN", "ghp_should_not_pass")
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        line = {"slug": SLUG, "revision": 4, "root_sha256": "r", "commit": "c", "changed": True}
        return subprocess.CompletedProcess(cmd, 0, "syncing…\n" + json.dumps(line) + "\n", "")

    monkeypatch.setattr(mod.subprocess, "run", run)
    doc = mod.stored_sync(SLUG)
    assert doc["revision"] == 4 and doc["changed"] is True
    assert seen["cmd"][1:] == ["agent", "sync", "--runner", "--slug", SLUG]
    assert seen["env"]["CANOPY_TOKEN"] == "runner-token" and "GH_TOKEN" not in seen["env"]


@pytest.mark.parametrize("rc,err", [(2, "Error: Missing option '--doc-url'."), (2, "Error: No such option '--runner'.")])
def test_an_old_cli_reads_as_too_old(cloud_runner, monkeypatch, rc, err):
    mod = cloud_runner
    monkeypatch.setattr(mod, "_bearer", lambda **_: "t")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, "", err))
    with pytest.raises(mod.StoredAgentError, match=mod.UPDATE_CANOPY_HINT.split(" (")[0]):
        mod.stored_sync(SLUG)


def test_any_other_cli_failure_is_not_called_too_old(cloud_runner, monkeypatch):
    mod = cloud_runner
    monkeypatch.setattr(mod, "_bearer", lambda **_: "t")
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Error: HTTP 503"))
    with pytest.raises(mod.StoredAgentError) as exc:
        mod.stored_sync(SLUG)
    assert mod.UPDATE_CANOPY_HINT not in str(exc.value)


# ── the plugins check ─────────────────────────────────────────────────────────

def test_the_plugins_check_sees_a_stored_plugin_behind_its_synced_revision(box, tmp_path, monkeypatch):
    mod, agents, _ = box
    old = materialize(agents / SLUG, 1, REV1)
    materialize(agents / SLUG, 2, REV2)
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {f"{SLUG}@{SLUG}": [{"gitCommitSha": old, "version": "0.1.0"}]}}))
    (plugins / "known_marketplaces.json").write_text(json.dumps(
        {SLUG: {"installLocation": str(agents / SLUG)}}))
    monkeypatch.setattr(mod, "_clone_moved_at", lambda loc: 0.0)
    mod._plugins_check(now=10 ** 9, home=home)
    assert mod._HEALTH["plugins"]["status"] == "warn"
    assert "is not the revision synced" in mod._HEALTH["plugins"]["detail"]
