"""A turn's chrome-sales acts in Salesforce only as the identity in its agent's vault.

Agents act in Salesforce as one agent's own login (Eva's), held in a workspace vault
attached to each agent that uses it, so it arrives among the agent's resolved values as
`salesforce`. The runner writes it to ~/.canopy/delegated/<slug>/chrome-sales/ and points
THAT turn's CHROME_SALES_HOME at it. Pinned: per agent, never the box's ~/.chrome-sales/,
and a vault detached from the agent removes the file rather than leaving it to act.
"""
from __future__ import annotations

import json
import stat

CREDS = json.dumps({"clientId": "PlatformCLI", "refreshToken": "r1", "instanceUrl": "https://x"})


def _values(cloud_runner, monkeypatch, tmp_path, values):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {}))
    monkeypatch.setattr(cloud_runner, "secrets_off_disk", lambda: True)
    monkeypatch.setattr(cloud_runner, "_turn_values", lambda slug, **k: dict(values))


def test_the_vault_value_is_staged_for_that_agent_only(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROME_SALES_HOME", "/box/wide")
    _values(cloud_runner, monkeypatch, tmp_path, {"salesforce": CREDS})
    env = cloud_runner._agent_env("echo")
    home = tmp_path / ".canopy" / "delegated" / "echo" / "chrome-sales"
    assert env["CHROME_SALES_HOME"] == str(home)
    f = home / ".sf-creds.json"
    assert json.loads(f.read_text())["refreshToken"] == "r1"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_no_value_means_no_chrome_sales_home_and_no_stale_file(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROME_SALES_HOME", "/box/wide")
    stale = tmp_path / ".canopy" / "delegated" / "echo" / "chrome-sales" / ".sf-creds.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(CREDS)
    _values(cloud_runner, monkeypatch, tmp_path, {})
    env = cloud_runner._agent_env("echo")
    assert "CHROME_SALES_HOME" not in env       # not the box's either
    assert not stale.exists()                   # a detached vault stops acting


def test_an_on_disk_env_file_serves_too(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "secrets_off_disk", lambda: False)
    (tmp_path / ".echo").mkdir()
    (tmp_path / ".echo" / ".env").write_text("X=1\nsalesforce='" + CREDS + "'\n")
    assert json.loads(cloud_runner._salesforce_value("echo"))["refreshToken"] == "r1"


# ── saying why a turn has no usable Salesforce login (failure investigation 38) ──

def _check(cloud_runner, name):
    return cloud_runner._HEALTH.get(name) or {}


def test_a_login_with_no_access_token_is_staged_with_a_warning(cloud_runner, monkeypatch, tmp_path):
    # chrome-sales answers "Not authenticated" for a file with no accessToken; the box
    # must say that is what it staged, by key names only.
    _values(cloud_runner, monkeypatch, tmp_path, {"salesforce": CREDS})
    cloud_runner._agent_env("echo")
    c = _check(cloud_runner, "salesforce.echo")
    assert c["status"] == "warn" and "accessToken" in c["detail"] and "refresh token" in c["detail"]
    assert "r1" not in c["detail"]


def test_a_usable_login_is_ok(cloud_runner, monkeypatch, tmp_path):
    full = json.dumps({"accessToken": "a1", "instanceUrl": "https://x", "refreshToken": "r1"})
    _values(cloud_runner, monkeypatch, tmp_path, {"salesforce": full})
    cloud_runner._agent_env("echo")
    c = _check(cloud_runner, "salesforce.echo")
    assert c["status"] == "ok" and "a1" not in c["detail"]


def test_an_agent_without_salesforce_gets_no_check(cloud_runner, monkeypatch, tmp_path):
    _values(cloud_runner, monkeypatch, tmp_path, {})
    cloud_runner._TURN_VALUE_GAPS.pop("echo", None)
    cloud_runner._agent_env("echo")
    assert "salesforce.echo" not in cloud_runner._HEALTH


def test_a_vault_that_did_not_open_is_named(cloud_runner, monkeypatch, tmp_path):
    _values(cloud_runner, monkeypatch, tmp_path, {})
    cloud_runner._TURN_VALUE_GAPS["echo"] = cloud_runner._value_gaps(
        {"vault_layers": [{"vault": "Salesforce (Eva's account)", "opened": False,
                           "why": "this device holds no copy of its key"}]})
    cloud_runner._agent_env("echo")
    c = _check(cloud_runner, "salesforce.echo")
    assert c["status"] == "warn" and "Salesforce (Eva's account)" in c["detail"]


def test_value_gaps_reads_every_diagnostic(cloud_runner):
    cr = cloud_runner
    gaps = cr._value_gaps({
        "vault_layers": [{"vault": "A", "opened": True, "names": ["x"], "left_out": ["y"]},
                         {"vault": "B", "opened": False, "fix": "confirm this machine"}],
        "sealed_skipped": [{"name": "Z", "why": "sealed to 2 other devices"}]})
    assert gaps == ("vault 'A' left out y; vault 'B' not opened (confirm this machine); "
                    "Z: sealed to 2 other devices")
    assert cr._value_gaps({}) == ""
