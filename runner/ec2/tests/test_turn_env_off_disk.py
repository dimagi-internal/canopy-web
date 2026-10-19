"""Secrets off disk: each turn's env on tmpfs, gone when the turn ends (spec
2026-10-19-runner-isolation-units §4, §5, §6; plan Task 6.3).

* Review Focus 4: the turn's env file is gone after a turn that finished, raised,
  timed out or was stopped — and leftovers are swept at start.
* Review Focus 5: on a canopy CLI older than 0.2.736 (no `CANOPY_AGENT_ENV_FILE`
  readers), the legacy `~/.<slug>/.env` path is kept, so turns still read their PAT.
* The per-agent values are cached for the 5-minute TTL `cred sync` had.
* gog: each agent's own home and HKDF-derived password; a confined turn gets only
  its agent's three variables.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading

import pytest

TID = "0b6c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"


@pytest.fixture
def off_disk(cloud_runner, monkeypatch, tmp_path):
    """A runner whose canopy CLI is new enough, with a tmpfs root under tmp_path."""
    monkeypatch.delenv("RUNNER_SECRET_FILES", raising=False)
    root = tmp_path / "run" / str(os.getuid())
    root.parent.mkdir(parents=True)
    monkeypatch.setenv("CANOPY_TURN_ENV_ROOT", str(root))
    monkeypatch.setattr(cloud_runner, "_canopy_cli_version", lambda **k: (0, 2, 736))
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path / "agents"))
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    monkeypatch.setattr(cloud_runner, "_TURN_VALUES", {})
    return cloud_runner, root


# ---- the gate (Review Focus 5) ---------------------------------------------------

def test_an_old_canopy_cli_keeps_the_legacy_env_file_and_turns_read_their_pat(off_disk, monkeypatch, tmp_path):
    mod, root = off_disk
    monkeypatch.setattr(mod, "_canopy_cli_version", lambda **k: (0, 2, 735))
    (tmp_path / "home" / ".eva").mkdir(parents=True)
    (tmp_path / "home" / ".eva" / ".env").write_text("CANOPY_WEB_PAT=eva-pat\n")
    (tmp_path / "agents" / "eva").mkdir(parents=True)
    synced = []
    monkeypatch.setattr(mod, "_refresh_agent_env", lambda slug, tok, vault="": synced.append(slug))
    monkeypatch.setattr(mod, "_api", lambda *a, **k: (200, {"op_sa_token": ""}))
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **k: pytest.fail("no in-memory resolve on an old CLI"))
    assert mod.secrets_off_disk() is False
    assert "older than 0.2.736" in mod._HEALTH["secrets"]["detail"]
    env = mod._agent_env("eva")
    assert env["CANOPY_WEB_PAT"] == "eva-pat" and "CANOPY_AGENT_ENV_FILE" not in env
    assert synced == ["eva"]
    # prepare --runner keeps writing files, and the legacy file is NOT removed.
    assert mod._off_disk_env({"X": "1"}) == {"X": "1"}
    assert not root.exists() or list(root.iterdir()) == []
    assert (tmp_path / "home" / ".eva" / ".env").exists()


def test_an_unknown_cli_version_is_the_legacy_path(off_disk, monkeypatch):
    mod, _ = off_disk
    monkeypatch.setattr(mod, "_canopy_cli_version", lambda **k: None)
    assert mod.secrets_off_disk() is False


def test_the_escape_hatch_keeps_files(off_disk, monkeypatch):
    mod, _ = off_disk
    monkeypatch.setenv("RUNNER_SECRET_FILES", "1")
    assert mod.secrets_off_disk() is False


def test_the_cli_version_is_parsed_and_cached(cloud_runner, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "canopy, version 0.2.740\n", "")

    monkeypatch.setattr(cloud_runner.subprocess, "run", run)
    cloud_runner._forget_cli_version()
    assert cloud_runner._canopy_cli_version(now=1000.0) == (0, 2, 740)
    assert cloud_runner._canopy_cli_version(now=1100.0) == (0, 2, 740)
    assert len(calls) == 1
    assert cloud_runner._canopy_cli_version(now=1000.0 + cloud_runner.CLI_VERSION_TTL_SECONDS + 1)
    assert len(calls) == 2


def test_new_enough_and_a_tmpfs_root_turns_it_on(off_disk):
    mod, root = off_disk
    assert mod.secrets_off_disk() is True
    assert mod._HEALTH["secrets"]["status"] == "ok"
    env = mod._off_disk_env({"X": "1"})
    assert env["CANOPY_TURN_ENV_ROOT"] == str(root)   # prepare --runner writes no env files


# ---- the per-agent cache ------------------------------------------------------------

def test_values_are_resolved_in_memory_and_cached_for_the_ttl(off_disk, monkeypatch):
    mod, _ = off_disk
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw["env"]))
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"agent": "eva", "source": "canopy-web", "values": {"CANOPY_WEB_PAT": f"pat-{len(calls)}"}}), "")

    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod, "_bearer", lambda min_life=0: "crt_box")
    assert mod._turn_values("eva", now=1000.0) == {"CANOPY_WEB_PAT": "pat-1"}
    assert mod._turn_values("eva", now=1000.0 + mod._AGENT_ENV_TTL_SECONDS - 1) == {"CANOPY_WEB_PAT": "pat-1"}
    assert len(calls) == 1
    argv, env = calls[0]
    assert argv[1:] == ["cred", "env", "--slug", "eva", "--runner", "--agent-root", mod.AGENT_ROOT,
                        "--print-json"]
    assert env["CANOPY_TOKEN"] == "crt_box"          # the box's own token, never a PAT
    assert mod._turn_values("eva", now=1000.0 + mod._AGENT_ENV_TTL_SECONDS + 1) == {"CANOPY_WEB_PAT": "pat-2"}


def test_a_failed_refresh_keeps_the_last_good_values(off_disk, monkeypatch):
    mod, _ = off_disk
    mod._TURN_VALUES["eva"] = (0.0, {"CANOPY_WEB_PAT": "good"})
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "op: unreachable"))
    monkeypatch.setattr(mod, "_bearer", lambda min_life=0: "")
    assert mod._turn_values("eva", now=10_000.0) == {"CANOPY_WEB_PAT": "good"}
    assert mod._HEALTH["env.eva"]["status"] == "warn"


def test_off_disk_agent_env_never_reads_or_syncs_the_legacy_file(off_disk, monkeypatch, tmp_path):
    mod, _ = off_disk
    (tmp_path / "home" / ".eva").mkdir(parents=True)
    (tmp_path / "home" / ".eva" / ".env").write_text("CANOPY_WEB_PAT=stale\n")
    monkeypatch.setattr(mod, "_refresh_agent_env", lambda *a, **k: pytest.fail("cred sync writes ~/.eva/.env"))
    monkeypatch.setattr(mod, "_api", lambda *a, **k: (200, {}))
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **k: {"CANOPY_WEB_PAT": "fresh"})
    assert mod._agent_env("eva")["CANOPY_WEB_PAT"] == "fresh"


# ---- the turn's file (Review Focus 4) ------------------------------------------------

@pytest.fixture
def turn_box(off_disk, monkeypatch, tmp_path):
    mod, root = off_disk
    monkeypatch.setattr(mod, "_turn_values", lambda slug, **k: {"CANOPY_WEB_PAT": "eva-pat", "X": "1"})
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: threading.Event())
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_release_agent_worktree", lambda p: None)
    monkeypatch.setattr(mod, "_github_turn_env", lambda rid, turn: {})
    timeline = []
    monkeypatch.setattr(mod, "_api", lambda m, path, body=None, **k: timeline.append((path, body)) or (200, {}))
    with mod._TURNS_LOCK:
        mod._IN_FLIGHT[TID] = None
    yield mod, root, timeline
    with mod._TURNS_LOCK:
        mod._IN_FLIGHT.pop(TID, None)
    mod._clear_stop(TID)


def _turn():
    return {"id": TID, "agent_slug": "eva", "prompt": "do it", "origin_ref": {}}


def _ok(mod, seen):
    def execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        path = pathlib.Path(mod._agent_env("eva")["CANOPY_AGENT_ENV_FILE"])
        seen.append((path, path.read_text(), path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777))
        return True, "done", "sid"
    return execute


def _raises(mod, seen):
    def execute(*a, **k):
        seen.append(pathlib.Path(mod._agent_env("eva")["CANOPY_AGENT_ENV_FILE"]))
        raise RuntimeError("the executor blew up")
    return execute


def _stopped(mod, seen):
    def execute(prompt, turn_id, emit, **k):
        seen.append(pathlib.Path(mod._agent_env("eva")["CANOPY_AGENT_ENV_FILE"]))
        mod.request_stop(turn_id, "a person pressed stop")
        return False, "turn ended: cancelled", ""
    return execute


def _timed_out(mod, seen):
    def execute(*a, **k):
        seen.append(pathlib.Path(mod._agent_env("eva")["CANOPY_AGENT_ENV_FILE"]))
        return False, "runner error: turn timed out after 3600s", ""
    return execute


@pytest.mark.parametrize("ending", [_ok, _raises, _stopped, _timed_out])
def test_the_turn_env_file_is_gone_however_the_turn_ends(turn_box, monkeypatch, ending):
    mod, root, _ = turn_box
    seen = []
    monkeypatch.setattr(mod, "execute_prompt", ending(mod, seen))
    mod._run_turn("rid", _turn())
    assert seen, "the turn never ran"
    path = seen[0][0] if isinstance(seen[0], tuple) else seen[0]
    assert path.parent == root / TID
    assert not path.exists() and not (root / TID).exists()
    assert list(root.iterdir()) == []


def test_the_turn_env_file_is_private_and_holds_the_values(turn_box, monkeypatch):
    mod, root, _ = turn_box
    seen = []
    monkeypatch.setattr(mod, "execute_prompt", _ok(mod, seen))
    mod._run_turn("rid", _turn())
    path, text, mode, dir_mode = seen[0]
    assert mode == 0o600 and dir_mode == 0o700
    assert "CANOPY_WEB_PAT=eva-pat\n" in text and "X=1\n" in text


def test_a_confined_turn_gets_no_env_file(turn_box, monkeypatch):
    mod, root, _ = turn_box
    monkeypatch.setattr(mod, "write_turn_env", lambda *a: pytest.fail("a caller's turn gets no agent env"))
    turn = {**_turn(), "caller_context": {"profile": "confined", "capability": {"name": "ask", "bash": []}}}
    monkeypatch.setattr(mod, "execute_prompt", lambda *a, **k: (True, "ok", ""))
    monkeypatch.setattr(mod, "_native_settings", lambda *a, **k: None)
    mod._run_turn("rid", turn)


def test_leftovers_are_swept_at_start(off_disk):
    mod, root = off_disk
    root.mkdir(mode=0o700)
    (root / "dead-turn-1").mkdir()
    (root / "dead-turn-1" / "env").write_text("CANOPY_WEB_PAT=leaked\n")
    (root / "stray").write_text("x")
    assert mod.sweep_turn_envs() == 2
    assert list(root.iterdir()) == []


def test_a_root_that_is_not_ours_is_not_used(off_disk, monkeypatch, tmp_path):
    mod, root = off_disk
    target = tmp_path / "elsewhere"
    target.mkdir()
    root.symlink_to(target)
    assert mod._turn_env_root() is None


def test_legacy_secret_files_are_removed(off_disk, tmp_path):
    mod, _ = off_disk
    files = [tmp_path / "home" / ".eva" / ".env", tmp_path / "home" / ".eva" / ".gws-sa-key.json",
             tmp_path / "agents" / "eva" / ".env", tmp_path / "agents" / "eva" / ".gws-sa-key.json"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("secret")
    keep = tmp_path / "agents" / "eva" / ".env.tpl"
    keep.write_text("CANOPY_WEB_PAT=op://x/y")
    removed = mod.remove_legacy_secret_files(["eva", "../etc"])
    assert sorted(removed) == sorted(map(str, files))
    assert not any(f.exists() for f in files) and keep.exists()


# ---- gog: one home and password per agent --------------------------------------------

def test_gog_password_matches_canopys_vectors(cloud_runner):
    assert cloud_runner.gog_password(bytes(range(32)), "eva") == \
        "ab2166c77a1c7ae28294765e11a5cac5758470a68352a5e872402d33388047fc"
    assert cloud_runner.gog_password(bytes(32), "ace") == \
        "7ee5d739a4ccab4ff159f9157a857e887702c7abb5617194923ba61cb84ebf59"


@pytest.fixture
def unit_secret(cloud_runner, monkeypatch, tmp_path):
    secret = tmp_path / "dimagi.gog-secret"
    secret.write_bytes(bytes(range(32)))
    monkeypatch.setenv("CANOPY_GOG_SECRET_FILE", str(secret))
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path / "home")
    return secret


def test_each_agent_gets_its_own_gog_home_and_password(cloud_runner, unit_secret, tmp_path):
    eva, ace = cloud_runner.gog_env("eva"), cloud_runner.gog_env("ace")
    assert eva == {"GOG_HOME": str(tmp_path / "home" / ".gog" / "eva"), "GOG_KEYRING_BACKEND": "file",
                   "GOG_KEYRING_PASSWORD": "ab2166c77a1c7ae28294765e11a5cac5758470a68352a5e872402d33388047fc"}
    assert ace["GOG_KEYRING_PASSWORD"] != eva["GOG_KEYRING_PASSWORD"] and ace["GOG_HOME"].endswith("/ace")


def test_a_confined_mail_turn_gets_only_its_agents_three_variables(cloud_runner, unit_secret, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_child_safe_env",
                        lambda: {"PATH": "/bin", "GOG_KEYRING_PASSWORD": "box-wide", "GOG_HOME": "/box",
                                 "CANOPY_GOG_SECRET_FILE": str(unit_secret)})
    cap = {"name": "ask", "bash": ["canopy email reply"]}
    env = cloud_runner._confined_env("eva", cap, {})
    assert env["GOG_KEYRING_PASSWORD"] == cloud_runner.gog_env("eva")["GOG_KEYRING_PASSWORD"]
    assert env["GOG_HOME"].endswith("/.gog/eva") and env["GOG_KEYRING_BACKEND"] == "file"
    assert "CANOPY_GOG_SECRET_FILE" not in env and "box-wide" not in env.values()
    assert cloud_runner.gog_env("ace")["GOG_KEYRING_PASSWORD"] not in env.values()


def test_an_unreadable_unit_secret_gives_a_confined_turn_nothing(cloud_runner, unit_secret, monkeypatch):
    unit_secret.chmod(0o000)
    monkeypatch.setattr(cloud_runner, "_child_safe_env",
                        lambda: {"PATH": "/bin", "GOG_KEYRING_PASSWORD": "box-wide"})
    try:
        if os.access(unit_secret, os.R_OK):
            pytest.skip("running as root: the file stays readable")
        env = cloud_runner._confined_env("eva", {"bash": ["canopy email send"]}, {})
        assert not any(k.startswith("GOG_") for k in env)
    finally:
        unit_secret.chmod(0o400)


def test_without_a_unit_secret_a_confined_turn_keeps_todays_variables(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_child_safe_env",
                        lambda: {"PATH": "/bin", "GOG_KEYRING_PASSWORD": "box-wide", "GOG_KEYRING_BACKEND": "file"})
    env = cloud_runner._confined_env("eva", {"bash": ["canopy email send"]}, {})
    assert env["GOG_KEYRING_PASSWORD"] == "box-wide"


def test_the_inbox_poller_reads_each_mailbox_with_its_agents_password(cloud_runner, unit_secret, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    for slug in ("eva", "ace"):
        cloud_runner._gog_runner(slug)(["gog", "gmail", "search", "--account", f"{slug}@x"], capture_output=True)
    (eva_argv, eva_env), (ace_argv, ace_env) = calls
    assert eva_env["GOG_KEYRING_PASSWORD"] == cloud_runner.gog_env("eva")["GOG_KEYRING_PASSWORD"]
    assert ace_env["GOG_KEYRING_PASSWORD"] == cloud_runner.gog_env("ace")["GOG_KEYRING_PASSWORD"]
    assert eva_env["GOG_HOME"].endswith("/eva") and ace_env["GOG_HOME"].endswith("/ace")


def test_drain_inbox_probes_each_mailbox_in_its_agents_gog_home(cloud_runner, unit_secret, monkeypatch):
    probes = []

    class _Inbox:
        @staticmethod
        def search_threads(account, client, max_threads=15, runner=None):
            probes.append((account, runner))
            return []

        @staticmethod
        def check_inbox(client, slug, **kw):
            return {}

    class _Due:
        @staticmethod
        def take_pending(): return set()
        @staticmethod
        def due(boxes, stamps, **kw): return [s for s in boxes if s not in stamps]
        @staticmethod
        def discovered_by(slug, rung): return "poll"

    monkeypatch.setattr(cloud_runner, "_inbox_core", lambda: (_Inbox, _Due))
    monkeypatch.setattr(cloud_runner, "_agent_mailboxes", lambda: {
        "eva": {"account": "eva@x", "client": "canopy"}, "ace": {"account": "ace@x", "client": "canopy"}})
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    cloud_runner._INBOX_STAMPS.clear()
    cloud_runner._GOG_CLIENT_LIVE.clear()
    seen_env = []
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda argv, **kw: seen_env.append(kw["env"]) or subprocess.CompletedProcess(argv, 0, "", ""))
    cloud_runner._drain_inbox("rid")
    assert {a for a, _ in probes} == {"eva@x", "ace@x"}
    for account, runner in probes:
        runner(["gog"], capture_output=True)
    assert {e["GOG_HOME"].rsplit("/", 1)[-1] for e in seen_env} == {"eva", "ace"}
    cloud_runner._INBOX_STAMPS.clear()
    cloud_runner._GOG_CLIENT_LIVE.clear()
