"""A runner with no canopy CLI signs its own proof — and one with no PAT never invents one.

The downloadable install has no canopy CLI and was paired from the browser, so its
runner.json holds no token. It must buy its runner token with a DPoP proof it signs
itself (device_proof), only for the key status.json registers for THIS runner, and
until it can, send nothing rather than a PAT that does not exist.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")

from canopy_runner import device_key, device_proof, runner_token  # noqa: E402
from canopy_runner.client import Client  # noqa: E402
from canopy_runner.config import Config  # noqa: E402
from canopy_runner.runner_token import RunnerCredential  # noqa: E402

from .test_device_proof import verify_dpop  # noqa: E402

URL = "https://canopy.test/api/runner-sessions/"


@pytest.fixture(autouse=True)
def _no_cli(monkeypatch):
    monkeypatch.delenv("CANOPY_DEVICE_KEY", raising=False)
    monkeypatch.setattr(device_key, "canopy_cli", lambda home=None: None)


class Server:
    def __init__(self, status=201):
        self.status = status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body)))
        if self.status != 201:
            return self.status, '{"detail": "nope"}'
        return 201, json.dumps({"access_token": f"crt_{len(self.calls)}", "runner": "r1",
                                "expires_at": "2099-01-01T00:00:00Z"})


def _register(runner="r1"):
    key, _ = device_proof.ensure(allow_software=True)
    device_proof.write_status(device_proof.registered_status(key, key_id=3, runner_id=runner))
    return key


def test_no_cli_signs_natively_with_the_registered_key():
    key = _register()
    srv = Server()
    cred = RunnerCredential("https://canopy.test/", "", "r1", transport=srv,
                            clock=lambda: 1000.0)
    assert cred.bearer() == "crt_1"
    assert cred.bearer() == "crt_1", "reused, not re-bought"
    method, url, headers, body = srv.calls[0]
    assert (method, url, body) == ("POST", URL, {"runner": "r1"})
    assert "Authorization" not in headers
    hdr = verify_dpop(headers["DPoP"], method="POST", url=URL)
    assert device_proof.jwk_thumbprint(hdr["jwk"]) == key.jkt


def test_no_key_and_no_pat_is_an_empty_bearer_not_a_credential():
    srv = Server()
    cred = RunnerCredential("https://canopy.test", "", "r1", transport=srv)
    assert cred.bearer() == ""
    assert srv.calls == [], "nothing to prove with, so nothing is sent"


def test_a_key_registered_for_another_runner_is_not_used():
    _register(runner="someone-else")
    srv = Server()
    assert RunnerCredential("https://canopy.test", "", "r1", transport=srv).bearer() == ""
    assert srv.calls == []


def test_a_refused_proof_backs_off_and_keeps_the_pat_if_there_is_one():
    _register()
    srv = Server(status=401)
    now = {"t": 1000.0}
    cred = RunnerCredential("https://canopy.test", "owner-pat", "r1", transport=srv,
                            clock=lambda: now["t"])
    assert cred.bearer() == "owner-pat"
    assert cred.bearer() == "owner-pat"
    assert len(srv.calls) == 1
    now["t"] += runner_token.RETRY_SECONDS
    cred.bearer()
    assert len(srv.calls) == 2


def test_a_failing_cli_falls_through_to_the_native_proof(monkeypatch):
    import subprocess

    _register()
    srv = Server()
    fail = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "old canopy")  # noqa: E731
    cred = RunnerCredential("https://canopy.test", "", "r1", runner=fail, cli="/bin/canopy",
                            transport=srv)
    assert cred.bearer() == "crt_1"


def test_config_without_a_token_loads_and_the_client_sends_no_authorization(tmp_path):
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "https://canopy.test", "runner_id": "r1"}))
    cfg = Config.load(p)
    assert cfg.token == ""
    p.write_text(json.dumps({"base_url": "https://canopy.test", "runner_id": "r1", "token": ""}))
    cfg = Config.load(p)
    cred = RunnerCredential.for_config(cfg, transport=Server())
    assert cred.pat == "" and Client(cfg.base_url, cred).token == ""


def test_device_key_enrollment_never_runs_without_any_credential(tmp_path):
    from types import SimpleNamespace

    ran = []
    cfg = SimpleNamespace(runner_id="r1", token="", base_url="https://canopy.test")
    st = device_key.enroll(cfg, runner=lambda *a, **k: ran.append(a), home=tmp_path,
                           cli="/bin/canopy", bearer="")
    assert st == {"error": "no credential"} and ran == []


def test_a_native_exchange_that_cannot_connect_retries_soon():
    _register()
    calls = []
    up = {"v": False}
    srv = Server()

    def transport(method, url, headers, body):
        calls.append(url)
        if not up["v"]:
            raise OSError("[Errno 8] nodename nor servname provided, or not known")
        return srv(method, url, headers, body)

    now = {"t": 1000.0}
    cred = RunnerCredential("https://canopy.test", "", "r1", transport=transport,
                            clock=lambda: now["t"])
    assert cred.bearer() == ""
    up["v"] = True
    now["t"] += runner_token.UNREACHABLE_RETRY_SECONDS
    assert cred.bearer() == "crt_1"
    assert len(calls) == 2
