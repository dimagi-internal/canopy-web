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
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": ""}))
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
