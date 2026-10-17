"""The menu bar's view of canopy: every heartbeat records whether it was accepted."""

import json

import pytest

from canopy_runner import server_status
from canopy_runner.client import Client, ClientError


@pytest.fixture
def status_file(tmp_path):
    path = tmp_path / "server-status.json"
    server_status.configure(path)
    yield path
    server_status._path = None
    server_status._last_ok_at = None


def _client(monkeypatch, outcome):
    c = Client("https://canopy.example", "tok")

    def fake_call(method, path, body=None, retry=True):
        if isinstance(outcome, Exception):
            raise outcome
        return 200, outcome

    monkeypatch.setattr(c, "_call", fake_call)
    return c


def test_an_accepted_heartbeat_is_recorded(monkeypatch, status_file):
    _client(monkeypatch, {"paused": True}).heartbeat("r1", [])
    doc = json.loads(status_file.read_text())
    assert doc["ok"] is True and doc["paused"] is True
    assert doc["last_ok_at"] == doc["at"]


def test_a_revoked_token_is_recorded_with_its_status_and_last_good_beat(monkeypatch, status_file):
    _client(monkeypatch, {}).heartbeat("r1", [])
    good = json.loads(status_file.read_text())["at"]
    refused = ClientError("POST /runners/r1/heartbeat -> 401: b'auth'", status=401)
    with pytest.raises(ClientError):
        _client(monkeypatch, refused).heartbeat("r1", [])
    doc = json.loads(status_file.read_text())
    assert doc["ok"] is False and doc["status"] == 401
    assert doc["last_ok_at"] == good
    assert "401" in doc["detail"]


def test_a_network_fault_has_no_status(monkeypatch, status_file):
    with pytest.raises(ClientError):
        _client(monkeypatch, ClientError("-> timed out", transient=True)).heartbeat("r1", [])
    doc = json.loads(status_file.read_text())
    assert doc["ok"] is False and doc["status"] is None and doc["last_ok_at"] is None


def test_last_good_beat_survives_a_restart(monkeypatch, status_file):
    _client(monkeypatch, {}).heartbeat("r1", [])
    good = json.loads(status_file.read_text())["at"]
    server_status.configure(status_file)  # a restart reads it back
    with pytest.raises(ClientError):
        _client(monkeypatch, ClientError("-> 401", status=401)).heartbeat("r1", [])
    assert json.loads(status_file.read_text())["last_ok_at"] == good


def test_unconfigured_writes_nothing(monkeypatch, tmp_path):
    server_status._path = None
    _client(monkeypatch, {}).heartbeat("r1", [])
    assert not list(tmp_path.iterdir())
