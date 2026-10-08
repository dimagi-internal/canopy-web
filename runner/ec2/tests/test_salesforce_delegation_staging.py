"""A turn's chrome-sales acts in Salesforce only as the identity its agent BORROWS.

canopy-web#1291: agents act in Salesforce with delegated access to one agent's own
credential (Eva's). The runner writes the borrowed `.sf-creds.json` to
~/.canopy/delegated/<slug>/chrome-sales/ and points THAT turn's CHROME_SALES_HOME
at it. Pinned: per agent, never the box's ~/.chrome-sales/, and a withdrawn loan
removes the file rather than leaving it to act.
"""
from __future__ import annotations

import json
import stat

CREDS = json.dumps({"clientId": "PlatformCLI", "refreshToken": "r1", "instanceUrl": "https://x"})


def _resolve(cloud_runner, monkeypatch, tmp_path, creds):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_AGENT_SF", {})
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "K", "salesforce_creds": creds}))


def test_a_borrowed_credential_is_staged_for_that_agent_only(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROME_SALES_HOME", "/box/wide")
    _resolve(cloud_runner, monkeypatch, tmp_path, CREDS)
    env = cloud_runner._agent_env("hal")
    home = tmp_path / ".canopy" / "delegated" / "hal" / "chrome-sales"
    assert env["CHROME_SALES_HOME"] == str(home)
    f = home / ".sf-creds.json"
    assert json.loads(f.read_text())["refreshToken"] == "r1"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_no_loan_means_no_chrome_sales_home_and_no_stale_file(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROME_SALES_HOME", "/box/wide")
    stale = tmp_path / ".canopy" / "delegated" / "hal" / "chrome-sales" / ".sf-creds.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(CREDS)
    _resolve(cloud_runner, monkeypatch, tmp_path, "")
    env = cloud_runner._agent_env("hal")
    assert "CHROME_SALES_HOME" not in env       # not the box's either
    assert not stale.exists()                   # a withdrawn loan stops acting
