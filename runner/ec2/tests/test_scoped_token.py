"""A colleague's full-profile turn on the cloud runner reads canopy as them: the caller token
goes to a private file, the session is told only the turn id, and a failed write refuses the turn."""
from __future__ import annotations

import stat

import pytest

TID = "3f2b8c1e-0000-4000-8000-000000000003"


def test_the_token_is_left_privately_and_the_session_gets_only_the_turn_id(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "SCOPED_TOKEN_ROOT", tmp_path)
    env = cloud_runner._scoped_token_env({"id": TID, "mcp_token": "cct_abc"})
    assert env == {"CANOPY_SCOPED_TURN": TID}                 # an id, never the secret
    f = tmp_path / "turn" / f"{TID}.token"
    assert f.read_text() == "cct_abc"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_a_turn_with_no_scoped_token_gets_nothing(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "SCOPED_TOKEN_ROOT", tmp_path)
    assert cloud_runner._scoped_token_env({"id": TID}) == {}
    assert not list(tmp_path.rglob("*.token"))


def test_an_unwritable_root_refuses_the_turn_rather_than_run_on_the_boxs_pat(cloud_runner, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(cloud_runner, "SCOPED_TOKEN_ROOT", blocker)
    with pytest.raises(cloud_runner.ConfineError):
        cloud_runner._scoped_token_env({"id": TID, "mcp_token": "cct_abc"})


def test_a_hostile_turn_id_is_refused(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "SCOPED_TOKEN_ROOT", tmp_path)
    with pytest.raises(cloud_runner.ConfineError):
        cloud_runner._scoped_token_env({"id": "../../x", "mcp_token": "cct_abc"})


def test_the_token_is_removed_when_the_turn_ends(cloud_runner, monkeypatch, tmp_path):
    """Nothing the turn acted with stays on the box after it."""
    monkeypatch.setattr(cloud_runner, "SCOPED_TOKEN_ROOT", tmp_path)
    cloud_runner._scoped_token_env({"id": TID, "mcp_token": "cct_abc"})
    cloud_runner._remove_scoped_token(TID)
    assert not list(tmp_path.rglob("*.token"))
    cloud_runner._remove_scoped_token(TID)                   # already gone: no error
    cloud_runner._remove_scoped_token("../../x")             # never a path outside the root


# -- the shared needs-auth cache -------------------------------------------------

def test_a_remembered_canopy_needs_auth_is_forgotten_before_a_turn(cloud_runner, monkeypatch, tmp_path):
    """One session with no credential must not cost every later session canopy's tools."""
    import json

    monkeypatch.setattr(cloud_runner, "_login_dir", lambda key="default": tmp_path)
    cache = tmp_path / "mcp-needs-auth-cache.json"
    cache.write_text(json.dumps({"plugin:canopy:canopy-web": {"timestamp": 1},
                                 "plugin:other:thing": {"timestamp": 2}}))
    cloud_runner._clear_canopy_needs_auth()
    assert json.loads(cache.read_text()) == {"plugin:other:thing": {"timestamp": 2}}


def test_a_symlinked_cache_is_rewritten_in_place(cloud_runner, monkeypatch, tmp_path):
    """Every login's cache is a symlink to the one in ~/.claude; the link must survive."""
    import json

    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps({"plugin:canopy:canopy-web": {"timestamp": 1}}))
    login = tmp_path / "login"
    login.mkdir()
    (login / "mcp-needs-auth-cache.json").symlink_to(shared)
    monkeypatch.setattr(cloud_runner, "_login_dir", lambda key="default": login)
    cloud_runner._clear_canopy_needs_auth()
    assert (login / "mcp-needs-auth-cache.json").is_symlink()
    assert json.loads(shared.read_text()) == {}


def test_no_cache_or_a_bad_one_is_left_alone(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "_login_dir", lambda key="default": tmp_path)
    cloud_runner._clear_canopy_needs_auth()                  # absent: nothing to do
    (tmp_path / "mcp-needs-auth-cache.json").write_text("not json")
    cloud_runner._clear_canopy_needs_auth()
    assert (tmp_path / "mcp-needs-auth-cache.json").read_text() == "not json"
