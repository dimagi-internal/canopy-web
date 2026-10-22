"""A turn's own agent login (turn-scoped agent token, phase 1): asked with the box's runner
token, preferred over the agent's stored canopy-pat, and any refusal falls back to that PAT."""
from __future__ import annotations

import stat

import pytest

TID = "3f2b8c1e-0000-4000-8000-000000000007"
TURN = {"id": TID, "agent_slug": "eva"}


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "AGENT_TOKEN_ROOT", tmp_path / "agent-token")
    monkeypatch.setattr(cloud_runner, "_bearer", lambda min_life=0: "crt_box")
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    calls = []

    def answer(status, body):
        def _api(method, path, body_=None, **kw):
            calls.append((method, path))
            return status, body
        monkeypatch.setattr(cloud_runner, "_api", _api)

    cloud_runner.answer, cloud_runner.calls = answer, calls
    return cloud_runner


def _file(mod):
    return mod.AGENT_TOKEN_ROOT / "turn" / f"{TID}.token"


def test_the_turn_gets_its_own_login_in_env_and_a_private_file(box):
    box.answer(200, {"token": "turn-tok", "agent_slug": "eva", "turn_id": TID,
                     "expires_at": "2026-10-22T00:00:00Z"})
    assert box._agent_token_env("rid", TURN) == {"CANOPY_WEB_PAT": "turn-tok"}
    assert box.calls == [("POST", f"/runners/rid/turns/{TID}/agent-token")]
    f = _file(box)
    assert f.read_text() == "turn-tok"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    box._remove_agent_token(TID)  # the turn's end
    assert not f.exists()


@pytest.mark.parametrize("status,body", [
    (404, None),                                     # an older canopy-web, or not this box's turn
    (409, None),                                     # no login for this agent
    (0, None),                                       # unreachable
    (200, {"token": "x", "agent_slug": "hal"}),      # a login for some other agent
])
def test_any_refusal_falls_back_to_the_stored_pat(box, status, body):
    box.answer(status, body)
    assert box._agent_token_env("rid", TURN) == {}
    assert not _file(box).exists()


def test_without_a_runner_token_it_does_not_ask(box, monkeypatch):
    monkeypatch.setattr(box, "_bearer", lambda min_life=0: "a-persons-pat")
    box.answer(200, {"token": "x", "agent_slug": "eva"})
    assert box._agent_token_env("rid", TURN) == {}
    assert box.calls == []


@pytest.mark.parametrize("turn", [{"id": TID}, {"id": "../../x", "agent_slug": "eva"}])
def test_an_agentless_turn_or_a_hostile_id_gets_nothing(box, turn):
    box.answer(200, {"token": "x", "agent_slug": "eva"})
    assert box._agent_token_env("rid", turn) == {}
    assert box.calls == []


def test_opt_out(box, monkeypatch):
    monkeypatch.setenv("CANOPY_TURN_AGENT_TOKEN", "0")
    box.answer(200, {"token": "x", "agent_slug": "eva"})
    assert box._agent_token_env("rid", TURN) == {}


def test_the_turn_login_wins_over_the_agents_stored_pat(cloud_runner, monkeypatch, tmp_path):
    mod = cloud_runner
    monkeypatch.setattr(mod.pathlib.Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home" / ".eva").mkdir(parents=True)
    (tmp_path / "home" / ".eva" / ".env").write_text("CANOPY_WEB_PAT=stored-pat\n")
    monkeypatch.setattr(mod, "secrets_off_disk", lambda: False)
    monkeypatch.setattr(mod, "_refresh_agent_env", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_agent_op_token", lambda slug: "")
    monkeypatch.setattr(mod, "_stage_salesforce", lambda slug: None)
    monkeypatch.setattr(mod, "_log", lambda m: None)
    mod._TURN_ENV.confined = None
    mod._TURN_ENV.extra = {}
    assert mod._agent_env("eva")["CANOPY_WEB_PAT"] == "stored-pat"  # no turn login: as before
    mod._TURN_ENV.extra = {"CANOPY_WEB_PAT": "turn-tok"}
    try:
        assert mod._agent_env("eva")["CANOPY_WEB_PAT"] == "turn-tok"
    finally:
        mod._TURN_ENV.extra = {}
