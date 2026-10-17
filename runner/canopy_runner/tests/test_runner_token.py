"""The laptop runner signs in with its device key, not its owner's PAT.

On 2026-10-17 a runner's owner PAT was revoked in a token cleanup and the box
silently lost every turn it claimed. A runner now buys an hour-long token with a
proof of its approved device key (`canopy vault runner-session`, canopy-web#1594);
the PAT is only the fallback while no approved key can get one.
"""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest

from canopy_runner import runner_token
from canopy_runner.client import Client
from canopy_runner.runner_token import RunnerCredential


@pytest.fixture(autouse=True)
def _device_key_on(monkeypatch):
    monkeypatch.delenv("CANOPY_DEVICE_KEY", raising=False)


class _Cli:
    """A fake `canopy vault runner-session`: answers with the next token, or fails."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if not self.ok:
            return subprocess.CompletedProcess(argv, 1, "", "canopy-web refused the device proof (401)")
        body = {"access_token": f"crt_{len(self.calls)}", "runner": "r1",
                "expires_at": "2099-01-01T00:00:00Z"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")


def _cred(cli, clock=lambda: 1000.0):
    return RunnerCredential("https://canopy.test/", "owner-pat", "r1",
                            runner=cli, cli="/usr/bin/canopy", clock=clock)


def test_it_signs_in_with_the_device_key_and_reuses_the_token():
    cli = _Cli()
    cred = _cred(cli)
    assert cred.bearer() == "crt_1"
    assert cred.bearer() == "crt_1"
    assert cli.calls == [["/usr/bin/canopy", "vault", "runner-session", "--runner", "r1",
                          "--base-url", "https://canopy.test"]]


def test_it_renews_before_the_token_runs_out():
    cli = _Cli()
    now = {"t": 1000.0}
    cred = _cred(cli, clock=lambda: now["t"])
    cred.bearer()
    cred._exp = now["t"] + runner_token.RENEW_SECONDS - 1
    assert cred.bearer() == "crt_2"


def test_no_approved_key_falls_back_to_the_pat_without_retrying_every_call():
    cli = _Cli(ok=False)
    now = {"t": 1000.0}
    cred = _cred(cli, clock=lambda: now["t"])
    assert cred.bearer() == "owner-pat"
    assert cred.bearer() == "owner-pat"
    assert len(cli.calls) == 1
    now["t"] += runner_token.RETRY_SECONDS
    cred.bearer()
    assert len(cli.calls) == 2, "never retried after the back-off"


def test_no_canopy_cli_or_opted_out_is_the_pat(monkeypatch):
    monkeypatch.setattr(runner_token.device_key, "canopy_cli", lambda: None)
    cred = RunnerCredential("https://canopy.test", "owner-pat", "r1", runner=_Cli())
    assert cred.bearer() == "owner-pat"
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    cli = _Cli()
    assert _cred(cli).bearer() == "owner-pat" and cli.calls == []


def test_a_401_on_the_runner_token_gets_a_fresh_one_and_retries_once(monkeypatch):
    cli = _Cli()
    client = Client("https://canopy.test", _cred(cli))
    seen = []

    class _Ok(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        seen.append(req.get_header("Authorization"))
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.get_full_url(), 401, "x", {}, io.BytesIO(b"{}"))
        return _Ok(b'{"ok": true}')

    monkeypatch.setattr("canopy_runner.client.urllib.request.urlopen", urlopen)
    assert client._call("GET", "/runners/r1/streams") == (200, {"ok": True})
    assert seen == ["Bearer crt_1", "Bearer crt_2"]


def test_a_plain_pat_client_is_unchanged():
    assert Client("https://canopy.test", "owner-pat").token == "owner-pat"
