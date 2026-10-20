"""`canopy-runner pair --browser`: the keyless loopback device flow, against a fake canopy-web.

The approval's secret comes back through a REAL HTTP GET to the local loopback server —
the leg that makes a phished approval useless — so these tests drive that server for real.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cryptography")

from canopy_runner import browser_pair, device_proof  # noqa: E402
from canopy_runner.pair import PairError  # noqa: E402

from .test_device_proof import verify_dpop  # noqa: E402

BASE = "https://canopy.test"
PAIRINGS = BASE + "/api/harness/runner-pairings/"


class FakeCanopy:
    """Answers the pairing calls; checks every DPoP proof against the URL it is for."""

    def __init__(self, *, start=None, claim_waiting=202, claim_paired=201):
        self.start = start
        self.claim_waiting = claim_waiting
        self.claim_paired = claim_paired
        self.calls: list[tuple[str, dict]] = []
        self.port = None

    def __call__(self, method, url, headers, body):
        data = json.loads(body)
        self.calls.append((url, data))
        verify_dpop(headers["DPoP"], method="POST", url=url)
        assert "Authorization" not in headers
        if url == PAIRINGS:
            self.port = data["redirect_port"]
            jkt = device_proof.jwk_thumbprint(data["public_jwk"])
            assert jkt == device_proof.jwk_thumbprint(
                json.loads(device_proof.b64url_decode(headers["DPoP"].split(".")[0]))["jwk"])
            return self.start or (201, json.dumps({
                "id": "p-1", "user_code": "ABCD-EFGH",
                "verify_url": BASE + "/pair/ABCD-EFGH",
                "expires_at": "2099-01-01T00:00:00Z", "interval": 1}))
        assert url == PAIRINGS + "p-1/claim"
        if data.get("secret") == "s3cret":
            return self.claim_paired, json.dumps({
                "status": "paired", "runner_id": "r-77", "name": "hal-mbp",
                "workspace": "dimagi", "device_key_id": 42})
        if data.get("secret"):
            return 401, '{"detail": "no"}'
        return self.claim_waiting, '{"status": "waiting"}'


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _pair(tmp_path, canopy, *, open_browser):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg = home / ".canopy" / "runner.json"
    out = []
    rc = browser_pair.run_browser_pair(
        cfg, base_url=BASE, allow_software=True, home=home, users_root=tmp_path / "Users",
        transport=canopy, open_browser=open_browser, is_free=lambda p: True,
        live_cdp_port=lambda h: None, out=out.append)
    return rc, cfg, home, out


def test_approved_in_the_browser_writes_a_config_with_no_token(tmp_path):
    canopy = FakeCanopy()
    seen = {}

    def browser(url):
        # What the browser does after the person approves: canopy-web redirects it to
        # the loopback. A stranger's pairing id is ignored first.
        seen["url"] = url
        seen["pairing_json"] = json.loads(
            (tmp_path / "home" / ".canopy" / "pairing.json").read_text())
        port = canopy.port
        assert _get(f"http://127.0.0.1:{port}/paired?pairing=other&secret=x")[0] == 404
        status, page = _get(f"http://127.0.0.1:{port}/paired?pairing=p-1&secret=s3cret")
        seen["page"] = (status, page)
        return True

    rc, cfg, home, out = _pair(tmp_path, canopy, open_browser=browser)
    assert rc == 0
    assert seen["url"] == BASE + "/pair/ABCD-EFGH"
    assert seen["pairing_json"]["user_code"] == "ABCD-EFGH"
    assert seen["pairing_json"]["status"] == "pending"
    assert seen["page"][0] == 200 and "Paired" in seen["page"][1]
    assert any("code ABCD-EFGH" in line for line in out)
    assert not any("s3cret" in line for line in out), "the secret is never printed"

    start = canopy.calls[0][1]
    assert start["kind"] == "software" and start["platform"] in ("macos", "windows", "linux")
    assert start["ecdh_public_jwk"] and start["ecdh_store"] == "software"
    assert start["hostname"] and start["account"] and isinstance(start["has_emdash"], bool)
    assert canopy.calls[-1] == (PAIRINGS + "p-1/claim", {"secret": "s3cret"})

    conf = json.loads(cfg.read_text())
    assert conf["token"] == "" and conf["runner_id"] == "r-77" and conf["base_url"] == BASE
    assert conf["emdash_db"] == "" and conf["cdp_port"] == 9222 and conf["hook_port"] == 8787
    st = device_proof.read_status()
    key = device_proof.load()
    assert st["status"] == "active" and st["runner"] == "r-77" and st["id"] == 42
    assert st["jkt"] == key.jkt and st["kind"] == "software" and st["ecdh_jkt"]
    assert not (home / ".canopy" / "pairing.json").exists()
    # And the runner can now prove itself for that runner, natively.
    assert device_proof.proof_headers("POST", BASE + "/api/runner-sessions/", runner_id="r-77")


def test_declined_stops_with_a_clear_message(tmp_path):
    canopy = FakeCanopy(claim_waiting=401)
    with pytest.raises(PairError, match="declined or has expired"):
        _pair(tmp_path, canopy, open_browser=lambda url: True)
    assert not (tmp_path / "home" / ".canopy" / "runner.json").exists()
    pf = json.loads((tmp_path / "home" / ".canopy" / "pairing.json").read_text())
    assert pf["status"] == "failed"


def test_a_wrong_secret_is_refused(tmp_path):
    canopy = FakeCanopy()

    def browser(url):
        _get(f"http://127.0.0.1:{canopy.port}/paired?pairing=p-1&secret=forged")
        return True

    with pytest.raises(PairError, match="refused the pairing"):
        _pair(tmp_path, canopy, open_browser=browser)


def test_an_already_registered_key_reattaches_without_a_browser(tmp_path):
    canopy = FakeCanopy(start=(200, json.dumps({
        "status": "reattached", "runner_id": "r-5", "name": "old", "workspace": "w",
        "device_key_id": 9})))

    def browser(url):
        raise AssertionError("no browser for a reattach")

    rc, cfg, home, out = _pair(tmp_path, canopy, open_browser=browser)
    assert rc == 0 and json.loads(cfg.read_text())["runner_id"] == "r-5"
    assert device_proof.read_status()["runner"] == "r-5"
    assert len(canopy.calls) == 1


def test_an_existing_config_is_left_alone(tmp_path):
    home = tmp_path / "home"
    (home / ".canopy").mkdir(parents=True)
    (home / ".canopy" / "runner.json").write_text("{}")
    out = []
    rc = browser_pair.run_browser_pair(home / ".canopy" / "runner.json", home=home,
                                       transport=lambda *a: pytest.fail("no call"),
                                       out=out.append)
    assert rc == 0 and "already paired" in out[0]


def test_canopy_not_answering_is_a_pair_error(tmp_path):
    canopy = FakeCanopy(start=(503, "down"))
    with pytest.raises(PairError, match="did not start a pairing"):
        _pair(tmp_path, canopy, open_browser=lambda url: True)


def test_a_dead_token_reconnects_the_same_runner_and_keeps_its_config(tmp_path):
    canopy = FakeCanopy()
    home = tmp_path / "home"
    cfg = home / ".canopy" / "runner.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"base_url": BASE, "token": "@~/.claude/canopy/workbench-token",
                               "runner_id": "r-77", "cdp_port": 9230, "hook_port": 8790,
                               "mailboxes": {"hal@x": True}}))

    def browser(url):
        _get(f"http://127.0.0.1:{canopy.port}/paired?pairing=p-1&secret=s3cret")
        return True

    out = []
    rc = browser_pair.run_browser_pair(
        cfg, base_url=BASE, allow_software=True, home=home, users_root=tmp_path / "Users",
        transport=canopy, open_browser=browser, is_free=lambda p: True,
        live_cdp_port=lambda h: None, out=out.append, reconnect=True)
    assert rc == 0
    assert canopy.calls[0][1]["reconnect_runner"] == "r-77"
    conf = json.loads(cfg.read_text())
    assert conf["token"] == "" and conf["runner_id"] == "r-77"
    assert (conf["cdp_port"], conf["hook_port"], conf["mailboxes"]) == (9230, 8790, {"hal@x": True})
    st = device_proof.read_status()
    assert st["status"] == "active" and st["runner"] == "r-77"


def test_reconnect_needs_a_runner_to_name(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".canopy" / "runner.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"base_url": BASE, "token": "x"}))
    with pytest.raises(PairError, match="names no runner"):
        browser_pair.run_browser_pair(cfg, base_url=BASE, allow_software=True, home=home,
                                      transport=FakeCanopy(), reconnect=True, out=lambda s: None)
