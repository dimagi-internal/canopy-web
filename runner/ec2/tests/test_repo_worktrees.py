"""Every cloud turn that works on a repo gets its OWN worktree of it (#1131).

On 2026-10-05 four turns on cloud-ec2-1 shared one checkout, /opt/canopy-web: a
project session's commit was stranded there, a Hal turn's uncommitted change sat
on top of it, a stash crossed between turns and branches moved under each other.
Project turns had an empty scratch dir and agent turns had no sanctioned place
for another repo, so each found /opt/canopy-web with `find /`. These pin the fix:
a per-session/per-turn worktree off a shared clone, for project turns and for
anything an agent asks `canopy-repo-worktree` for, and a shared clone that
refuses commits.
"""
from __future__ import annotations

import subprocess

import pytest


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", msg, cwd=repo)


@pytest.fixture
def origins(tmp_path):
    """A local 'GitHub': <root>/<repo> holds a repo with a README on main."""
    root = tmp_path / "github"
    repo = root / "canopy-web"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _commit(repo, "v1")
    return root


@pytest.fixture
def runner(cloud_runner, monkeypatch, tmp_path, origins):
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path / "agents"))
    monkeypatch.setattr(cloud_runner, "REPO_URL_TEMPLATE", str(origins / "{repo}"))
    return cloud_runner


def _project_turn(sid=""):
    return {"project": "canopy-web", "origin_ref": {"chat_session_id": sid} if sid else {}}


# ── project turns run in their own worktree of the project ──────────────────

def test_a_project_turn_runs_in_a_worktree_of_its_repo(runner):
    cwd = runner._turn_cwd(_project_turn(), "aaaaaaaa-1")
    assert (cwd / "README.md").read_text() == "v1\n"
    # Same path as the scratch dir it replaces, so transcript lookups are unchanged.
    assert cwd == runner.pathlib.Path(runner.WORK_DIR) / "aaaaaaaa"


def test_two_project_turns_do_not_share_a_working_tree(runner):
    a = runner._turn_cwd(_project_turn(), "aaaaaaaa-1")
    b = runner._turn_cwd(_project_turn(), "bbbbbbbb-2")
    (a / "README.md").write_text("turn a's edit\n")
    assert a != b
    assert (b / "README.md").read_text() == "v1\n"


def test_a_project_session_keeps_one_worktree_across_turns(runner):
    a = runner._turn_cwd(_project_turn("sess-1"), "t1")
    b = runner._turn_cwd(_project_turn("sess-1"), "t2")
    assert a == b
    assert (a / "README.md").exists()
    assert "sessions" in a.parts


# ── canopy-repo-worktree: an agent's own checkout of another repo ────────────

def test_repo_worktree_is_keyed_and_stable(runner):
    a = runner.repo_worktree("canopy-web", "sess-hal")
    assert (a / "README.md").read_text() == "v1\n"
    assert runner.repo_worktree("canopy-web", "sess-hal") == a
    assert runner.repo_worktree("canopy-web", "sess-ada") != a


def test_repo_worktree_follows_origin_when_clean(runner, origins):
    a = runner.repo_worktree("canopy-web", "k")
    (origins / "canopy-web" / "README.md").write_text("v2\n")
    _commit(origins / "canopy-web", "v2")
    runner.repo_worktree("canopy-web", "k")
    assert (a / "README.md").read_text() == "v2\n"


def test_repo_worktree_never_moves_a_branch_with_work_on_it(runner, origins):
    # A clean status is not "no work": a turn that committed on a branch has a
    # clean tree and unpushed commits. Detaching it to origin/main mid-turn
    # would yank the agent off its own branch.
    a = runner.repo_worktree("canopy-web", "k")
    _git("checkout", "-qb", "feature", cwd=a)
    (a / "README.md").write_text("mine\n")
    _commit(a, "mine")
    (origins / "canopy-web" / "README.md").write_text("v2\n")
    _commit(origins / "canopy-web", "v2")
    runner.repo_worktree("canopy-web", "k")
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=a) == "feature"
    assert (a / "README.md").read_text() == "mine\n"


@pytest.mark.parametrize("bad", ["", "..", "../etc", "a/b", "-rf", "x.git", "a b"])
def test_repo_worktree_refuses_a_name_that_is_not_a_repo(runner, bad):
    with pytest.raises(ValueError):
        runner.repo_worktree(bad, "k")


def test_the_cli_prints_the_turns_own_worktree(runner, monkeypatch, capsys):
    monkeypatch.setenv("CANOPY_REPO_WORKTREE_KEY", "sess-hal")
    assert runner._repo_worktree_cli(["canopy-web"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == str(runner.repo_worktree("canopy-web", "sess-hal"))


def test_the_cli_refuses_without_a_turn_key(runner, monkeypatch, capsys):
    monkeypatch.delenv("CANOPY_REPO_WORKTREE_KEY", raising=False)
    assert runner._repo_worktree_cli(["canopy-web"]) != 0
    assert "CANOPY_REPO_WORKTREE_KEY" in capsys.readouterr().err


def test_a_turn_is_handed_its_worktree_key(runner):
    assert runner._repo_worktree_env(_project_turn("sess-9"), "tid-0001") == {
        "CANOPY_REPO_WORKTREE_KEY": "sess-9"}
    assert runner._repo_worktree_env({"agent_slug": "hal"}, "abcdef12-rest") == {
        "CANOPY_REPO_WORKTREE_KEY": "abcdef12"}


def test_the_helper_is_installed_on_path(runner, tmp_path):
    bindir = tmp_path / "bin"
    shim = runner._install_repo_worktree_shim(bindir)
    assert shim == bindir / "canopy-repo-worktree"
    body = shim.read_text()
    assert "repo-worktree" in body and str(runner.__file__) in body
    assert shim.stat().st_mode & 0o111


# ── the shared clone refuses commits ────────────────────────────────────────

def test_the_shared_clone_refuses_commits_and_says_where_to_go(runner, origins, tmp_path):
    shared = tmp_path / "opt-canopy-web"
    _git("clone", "-q", str(origins / "canopy-web"), str(shared), cwd=tmp_path)
    runner._guard_shared_clone(shared)
    (shared / "README.md").write_text("stray\n")
    res = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                          "commit", "-qam", "stray"], cwd=shared, capture_output=True, text=True)
    assert res.returncode != 0
    assert "canopy-repo-worktree canopy-web" in res.stderr


def test_guarding_a_missing_clone_is_a_no_op(runner, tmp_path):
    runner._guard_shared_clone(tmp_path / "absent")  # must not raise
