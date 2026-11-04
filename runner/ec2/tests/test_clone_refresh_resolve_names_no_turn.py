"""The runner's own read of the agent owner's GitHub delegation names no turn.

trellis on cloud-ec2-live, 2026-11-04: GET /api/agents/trellis/readiness said "could not read
Username" while the delegation was healthy. The box's resolve of trellis carried the confined
turn's id; the turn was started by the runner's OWNER as an editor (`acts_as`), so canopy-web
capped the owner on that request, made them no admin of the agent and answered 403 "does not run
trellis". `_clone_refresh_env` took that as no delegation and fetched with no token. These tests
run the real `_turn_values` and `_clone_refresh_env` against a stand-in canopy CLI that behaves
that way.
"""
from __future__ import annotations

import json
import stat

import pytest

TOKEN = "ghp" + "_FAKEcanopyTESTtoken0123456789abcdefXYZ"   # assembled: nothing token-shaped to flag
SLUG = "trellis"

# Refuses (403) when the request would name a turn, as the capped owner is refused; otherwise
# answers with a delegation. Records the environment it was started with.
_CLI = """#!/bin/sh
env > "@LOG@"
if [ -n "$CANOPY_TURN_ID" ]; then
  echo 'GET /api/agents/trellis/credentials/resolve -> 403: cloud-ec2-live does not run trellis' >&2
  exit 1
fi
echo '{"values": {}, "delegated": {"github-token": "@TOKEN@"}}'
"""


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    mod = cloud_runner
    cli = tmp_path / "canopy"
    log = tmp_path / "cli-env.txt"
    cli.write_text(_CLI.replace("@LOG@", str(log)).replace("@TOKEN@", TOKEN))
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(mod, "_canopy_cli", lambda: str(cli))
    monkeypatch.setattr(mod, "_bearer", lambda **_: "runner-bearer")
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", False)
    monkeypatch.setattr(mod, "_TURN_VALUES", {})
    monkeypatch.setattr(mod, "_TURN_ENV_ERRORS", {})
    monkeypatch.setattr(mod, "_AGENT_DELEGATED", {})
    monkeypatch.setattr(mod, "_HEALTH", {})
    monkeypatch.setattr(mod, "_CLONE_FETCH", {})
    monkeypatch.setattr(mod, "_set_check",
                        lambda name, status, detail="": mod._HEALTH.__setitem__(name, (status, detail)))
    monkeypatch.setattr(mod._TURN_ENV, "extra", {"CANOPY_TURN_ID": "capped-confined-turn"}, raising=False)
    return mod, log, {"PATH": "/usr/bin:/bin", "CANOPY_AGENT": SLUG}


def test_the_turns_own_read_names_the_turn_and_is_refused_as_the_capped_owner(box):
    mod, log, _ = box
    assert mod._turn_values(SLUG) is None
    assert "does not run trellis" in mod._TURN_ENV_ERRORS[SLUG]
    assert "CANOPY_TURN_ID=capped-confined-turn" in log.read_text()


def test_the_clone_refresh_read_names_no_turn_and_gets_the_delegation(box):
    mod, log, confined_env = box
    fetch_env = mod._clone_refresh_env(SLUG, confined_env)
    assert fetch_env["GH_TOKEN"] == TOKEN
    assert "CANOPY_TURN_ID" not in log.read_text()
    assert not mod._TURN_ENV_ERRORS.get(SLUG)


def test_the_delegation_stays_out_of_the_turns_env_and_the_health_payload(box):
    mod, _, confined_env = box
    mod._clone_refresh_env(SLUG, confined_env)
    assert TOKEN not in json.dumps(confined_env)
    assert TOKEN not in json.dumps(mod._HEALTH)


def test_a_resolve_that_fails_outside_a_turn_still_means_no_token(box, monkeypatch, tmp_path):
    mod, _, confined_env = box
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\necho 'resolve -> 500' >&2\nexit 1\n")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(mod, "_canopy_cli", lambda: str(broken))
    assert mod._clone_refresh_env(SLUG, confined_env) is confined_env


def test_the_clones_check_says_what_actually_kept_the_token_away(box):
    mod, _, _ = box
    mod._TURN_ENV_ERRORS[SLUG] = "GET …/credentials/resolve -> 403"
    assert "could not read trellis's credentials" in mod._clone_refresh_blocker(SLUG)
    mod._TURN_ENV_ERRORS.clear()
    assert "lists no GitHub delegation" in mod._clone_refresh_blocker(SLUG)
    mod._AGENT_DELEGATED[SLUG] = {"github-token": TOKEN}
    assert mod._clone_refresh_blocker(SLUG) == ""
    mod.SENSITIVE_DATA_MODE = True
    assert "sensitive-data box" in mod._clone_refresh_blocker(SLUG)
    mod.SENSITIVE_DATA_MODE = False
    mod._CLONE_FETCH["/agents/trellis"] = {"ok_at": 0.0, "failed_at": 1.0,
                                           "error": "fatal: could not read Username"}
    mod._AGENT_DELEGATED.clear()
    mod._TURN_ENV_ERRORS[SLUG] = "403"
    mod._clones_check(1e12)
    status, detail = mod._HEALTH["clones"]
    assert status == "warn" and "could not read trellis's credentials" in detail and TOKEN not in detail
