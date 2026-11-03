"""Where the agent owner's GitHub delegation exists while the runner refreshes an agent's clone.

CLAUDE.md: no box holds a GitHub token; a turn gets its agent owner's delegation per turn and
NEVER a confined (caller's) turn. The runner's own `git fetch` of the agent's clone for a new
chat session uses the delegation for housekeeping (`_clone_refresh_env`); these tests hold the
rule that it lives only in that one git process's environment:

* not in the confined turn's env, the runner's env, or any git invocation but fetch / pull;
* not in a git argument, the clone's .git/config or remote URL, or any file in the clone or the
  session worktree (the worktree's gitdir points at the clone, so both are read-reachable);
* not in the logged error or the `clones` health payload, even when git echoes it;
* only while this box's own resolve still lists the delegation (a withdrawn one is not used).
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest

TOKEN = "ghp_FAKEcanopyTESTtoken0123456789abcdefXYZ"
SLUG = "trellis"
CHAT = {"agent_slug": SLUG, "origin_ref": {"chat_session_id": "sess-1"}}

# Records every git invocation (argv + whether the token was in its env) OUTSIDE the trees
# the tests scan, then runs the real git — or, with FAKE_GIT_FAIL, fails the way a bad
# fetch does (when a `fail` file sits beside it) while ECHOING the token and a URL with
# credentials in it.
_SHIM = """#!/bin/sh
python3 - "$@" <<'PY'
import json, os, sys
rec = {"argv": sys.argv[1:], "gh": os.environ.get("GH_TOKEN", ""), "github": os.environ.get("GITHUB_TOKEN", ""),
       "cfg": os.environ.get("GIT_CONFIG_COUNT", "")}
open("@LOG@", "a").write(json.dumps(rec) + "\\n")
PY
if [ -e "@FAIL@" ]; then
  case " $* " in
    *" fetch "*|*" pull "*)
      echo "fatal: unable to access 'https://x-access-token:${GH_TOKEN}@github.com/o/r.git/': 403 (token ${GH_TOKEN})" >&2
      exit 128;;
  esac
fi
exec /usr/bin/git "$@"
"""


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=repo)


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "SKILL.md").write_text("v1\n")
    _commit(origin, "v1")
    _git("config", "receive.denyCurrentBranch", "ignore", cwd=origin)
    agents = tmp_path / "agents"
    agents.mkdir()
    _git("clone", "-q", str(origin), str(agents / SLUG), cwd=tmp_path)
    shim_dir = tmp_path / "shim"          # outside the scanned trees
    shim_dir.mkdir()
    git = shim_dir / "git"
    log = shim_dir / "git-calls.jsonl"
    log.write_text("")
    # The shim names its own paths: a turn's env is an explicit allowlist, so nothing
    # can be passed to it through the environment.
    git.write_text(_SHIM.replace("@LOG@", str(log)).replace("@FAIL@", str(shim_dir / "fail")))
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    mod = cloud_runner
    monkeypatch.setattr(mod, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(mod, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(mod, "_CLONE_FETCH", {})
    monkeypatch.setattr(mod, "_HEALTH", {})
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", False)
    # This box's own resolve lists the delegation, as it would for an agent whose owner lent one.
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **_: {})
    monkeypatch.setattr(mod, "_TURN_ENV_ERRORS", {})
    monkeypatch.setattr(mod, "_AGENT_DELEGATED", {SLUG: {"github-token": TOKEN}})
    confined_env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "CANOPY_AGENT": SLUG}
    return mod, origin, agents / SLUG, tmp_path, log, confined_env


def _calls(log):
    return [json.loads(line) for line in pathlib.Path(log).read_text().splitlines() if line.strip()]


def _subcommand(argv):
    rest = list(argv)               # `git -C <path> <cmd> …`
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    return rest[0] if rest else ""


def _files_with(root, needle: str):
    hits = []
    for path in pathlib.Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                if needle.encode() in path.read_bytes():
                    hits.append(str(path))
            except OSError:
                pass
    return hits


def test_a_confined_turns_refresh_puts_the_token_in_the_fetch_process_and_nowhere_else(box):
    mod, origin, clone, tmp, log, confined_env = box
    (origin / "SKILL.md").write_text("v2\n")
    _commit(origin, "v2")
    before = dict(confined_env)

    cwd = mod._turn_cwd(CHAT, "t1", env=confined_env)

    assert (cwd / "SKILL.md").read_text() == "v2\n"            # the refresh worked
    calls = _calls(log)
    carrying = [c for c in calls if c["gh"] or c["github"]]
    assert carrying, f"the refresh should have used the delegation (else this test proves nothing): {calls} {mod._CLONE_FETCH}"
    # Only fetch / pull in the CLONE ever saw it; worktree add, checkout, status, … did not.
    assert {_subcommand(c["argv"]) for c in carrying} <= {"fetch", "pull"}
    for c in calls:
        assert TOKEN not in " ".join(c["argv"])                # never an argument (visible in `ps`)
    # The confined turn's own environment — the dict and the process — never held it.
    assert confined_env == before
    assert TOKEN not in json.dumps(confined_env) and TOKEN not in json.dumps(dict(os.environ))
    assert "GH_TOKEN" not in os.environ and "GIT_CONFIG_COUNT" not in os.environ
    extra = getattr(mod._TURN_ENV, "extra", None) or {}
    assert TOKEN not in json.dumps(extra)
    # What the confined session is started with, built after the refresh.
    started = mod._confined_env(SLUG, {"name": "none", "tools": [], "bash": [], "read_paths": [],
                                      "write_paths": [], "entry": None}, dict(extra))
    assert TOKEN not in json.dumps(started)
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT"} & set(started)


def test_no_file_in_the_clone_or_the_session_worktree_holds_the_token(box):
    mod, origin, clone, tmp, log, confined_env = box
    cwd = mod._turn_cwd(CHAT, "t1", env=confined_env)
    (origin / "SKILL.md").write_text("v2\n")
    _commit(origin, "v2")
    assert mod._turn_cwd(CHAT, "t2", env=confined_env) == cwd   # a second refresh, worktree exists
    assert any(c["gh"] for c in _calls(log))
    # The clone (.git/config, FETCH_HEAD, worktrees/, logs, …), the session worktree and the
    # whole box tree: a confined `Read {cwd}/**` and the worktree's gitdir reach no token.
    assert _files_with(tmp / "agents", TOKEN) == []
    assert _files_with(tmp / "work", TOKEN) == []
    assert _files_with(tmp / "home", TOKEN) == [] if (tmp / "home").exists() else True
    remote = _git("config", "--get-regexp", r"^remote\..*", cwd=clone)
    assert TOKEN not in remote and "@" not in _git("remote", "get-url", "origin", cwd=clone)
    assert TOKEN not in _git("config", "--list", "--show-origin", cwd=cwd)   # via the worktree too
    assert TOKEN not in (cwd / ".git").read_text()           # the worktree's pointer to the clone


def test_a_failing_fetch_that_echoes_the_token_is_redacted_in_the_log_the_record_and_the_health(box, monkeypatch):
    mod, _, clone, tmp, log, confined_env = box
    logs: list[str] = []
    monkeypatch.setattr(mod, "_log", logs.append)
    (tmp / "shim" / "fail").write_text("")

    mod._turn_cwd(CHAT, "t1", env=confined_env)

    assert any(c["gh"] for c in _calls(log))                  # it did run with the token
    assert any("could not update" in line for line in logs)
    now = mod._CLONE_FETCH[str(clone)]["failed_at"]
    mod._clones_check(now + mod.CLONE_STALE_SECONDS + 3600)
    check = mod._HEALTH["clones"]
    assert check["status"] == "warn"
    surfaces = logs + [json.dumps(mod._CLONE_FETCH), json.dumps(mod._HEALTH)]
    for text in surfaces:
        assert TOKEN not in text, text
        assert "x-access-token:" not in text, text            # the URL's userinfo goes too
    assert "403" in check["detail"]                           # still says what failed


def test_redaction_catches_a_token_by_value_by_shape_and_in_a_url(box):
    mod = box[0]
    assert TOKEN not in mod._redact_credentials(f"bad {TOKEN} here", {"GH_TOKEN": TOKEN})
    assert "opaque-secret-value" not in mod._redact_credentials("x opaque-secret-value y", {"GITHUB_TOKEN": "opaque-secret-value"})
    assert "ghp_" not in mod._redact_credentials("tok ghp_abcdefghijklmnopqrstuvwxyz0123", None)
    assert "github_pat_" not in mod._redact_credentials("tok github_pat_11ABCDEFG0123456789_abcdefghij", None)
    assert mod._redact_credentials("fatal: https://u:p4ss@github.com/o/r.git/ 403", None) == \
        "fatal: https://***@github.com/o/r.git/ 403"
    assert mod._redact_credentials("fatal: repository not found", None) == "fatal: repository not found"


def test_a_delegation_this_boxs_resolve_no_longer_lists_is_not_used(box, monkeypatch):
    mod, _, _, _, log, confined_env = box

    def withdrawn(slug, **_):                       # the resolve now reports no delegation
        mod._AGENT_DELEGATED[slug] = {}
        return {}
    monkeypatch.setattr(mod, "_turn_values", withdrawn)
    assert mod._clone_refresh_env(SLUG, confined_env) is confined_env
    mod._turn_cwd(CHAT, "t1", env=confined_env)
    assert not any(c["gh"] for c in _calls(log))


def test_a_cached_token_is_not_used_when_the_resolve_that_would_confirm_it_failed(box, monkeypatch):
    mod, _, _, _, log, confined_env = box
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **_: {"cached": "yes"})   # stale last-good values
    mod._TURN_ENV_ERRORS[SLUG] = "the canopy CLI could not resolve"
    assert mod._clone_refresh_env(SLUG, confined_env) is confined_env
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **_: None)
    mod._TURN_ENV_ERRORS.clear()
    assert mod._clone_refresh_env(SLUG, confined_env) is confined_env


def test_a_sensitive_data_box_or_a_turn_with_its_own_identity_borrows_nothing(box, monkeypatch):
    mod, _, _, _, log, confined_env = box
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", True)
    mod._turn_cwd(CHAT, "t1", env=confined_env)
    assert not any(c["gh"] for c in _calls(log))
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", False)
    own = {"GH_TOKEN": "the-turns-own"}
    assert mod._clone_refresh_env(SLUG, own) is own
