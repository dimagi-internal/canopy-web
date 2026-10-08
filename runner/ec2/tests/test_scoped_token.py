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
