"""The runner's credential resolve names the turn it is for (Canopy Vault, canopy#946).

canopy-web#1538 logs every `/credentials/resolve` read with the caller, the runner and —
when sent — `X-Canopy-Turn-Id`. A turn's thread knows its id (`_lineage_env` puts
CANOPY_TURN_ID in the turn env); a resolve outside a turn sends no header, and an older
canopy-web simply ignores it."""
from __future__ import annotations


def _capture(cloud_runner, monkeypatch):
    seen = []

    def api(method, path, body=None, *, prefix="/api/harness", headers=None):
        seen.append((path, dict(headers or {})))
        return 200, {"zk": True}

    monkeypatch.setattr(cloud_runner, "_api", api)
    cloud_runner._VAULT_PROBE_AT.clear()
    return seen


def test_a_turns_resolve_carries_its_turn_id(cloud_runner, monkeypatch):
    seen = _capture(cloud_runner, monkeypatch)
    cloud_runner._TURN_ENV.extra = {"CANOPY_TURN_ID": "t-946"}
    try:
        cloud_runner._probe_agent_vault("eva")
    finally:
        cloud_runner._TURN_ENV.extra = {}
    (path, headers), = seen
    assert path == "/eva/credentials/resolve" and headers["X-Canopy-Turn-Id"] == "t-946"


def test_outside_a_turn_no_turn_header_is_sent(cloud_runner, monkeypatch):
    seen = _capture(cloud_runner, monkeypatch)
    cloud_runner._probe_agent_vault("eva")
    assert seen[0][1].get("X-Canopy-Turn-Id", "") == ""


def test_api_adds_only_non_empty_headers(cloud_runner, monkeypatch):
    sent = {}

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.example")
    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen", urlopen)
    cloud_runner._api("GET", "/x", headers={"X-Canopy-Turn-Id": "t-1", "DPoP": ""})
    assert sent["x-canopy-turn-id"] == "t-1" and "dpop" not in sent
