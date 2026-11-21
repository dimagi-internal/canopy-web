"""`stored_agents.ensure_checkout`: a stored agent's laptop checkout, through a faked CLI."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from canopy_runner import stored_agents


def _cli(tmp_path, monkeypatch):
    monkeypatch.setattr(stored_agents, "canopy_cli", lambda home=None: "/stub/canopy")


def _fake(tmp_path, rc=0, out="", err="", make=True):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "/stub/canopy" and make and rc == 0:
            dest = Path(cmd[cmd.index("--dest") + 1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, rc, out, err)
    return run, calls


def test_it_materializes_into_the_persons_agents_dir(tmp_path, monkeypatch):
    _cli(tmp_path, monkeypatch)
    line = json.dumps({"slug": "connect-helper", "revision": 3, "root_sha256": "r", "commit": "c",
                       "changed": False})
    run, calls = _fake(tmp_path, out=line + "\n")
    dest = stored_agents.ensure_checkout("connect-helper", run=run, home=tmp_path)
    assert dest == tmp_path / ".canopy" / "agents" / "connect-helper"
    assert calls == [["/stub/canopy", "agent", "materialize", "--slug", "connect-helper",
                      "--dest", str(dest)]]


@pytest.mark.parametrize("err", ["Error: No such command 'materialize'.", "Error: No such option '--dest'."])
def test_an_old_cli_fails_with_the_update_hint(tmp_path, monkeypatch, err):
    _cli(tmp_path, monkeypatch)
    run, _ = _fake(tmp_path, rc=2, err=err)
    with pytest.raises(stored_agents.StoredAgentError, match="update canopy"):
        stored_agents.ensure_checkout("connect-helper", run=run, home=tmp_path)


def test_no_cli_fails_with_the_update_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(stored_agents, "canopy_cli", lambda home=None: None)
    with pytest.raises(stored_agents.StoredAgentError, match="no canopy CLI"):
        stored_agents.ensure_checkout("connect-helper", run=lambda *a, **k: None, home=tmp_path)


def test_a_cli_that_printed_no_revision_is_not_trusted(tmp_path, monkeypatch):
    _cli(tmp_path, monkeypatch)
    run, _ = _fake(tmp_path, out="done\n")
    with pytest.raises(stored_agents.StoredAgentError, match="left no checkout"):
        stored_agents.ensure_checkout("connect-helper", run=run, home=tmp_path)


def test_a_new_revision_updates_the_installed_plugin(tmp_path, monkeypatch):
    _cli(tmp_path, monkeypatch)
    from canopy_runner import desktop

    monkeypatch.setattr(desktop, "claude_cli", lambda: Path("/stub/claude"))
    monkeypatch.setattr(desktop, "_cli_env", lambda claude: {})
    dest = tmp_path / ".canopy" / "agents" / "connect-helper"
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "connect-helper"}))
    line = json.dumps({"slug": "connect-helper", "revision": 4, "commit": "c", "changed": True})
    run, calls = _fake(tmp_path, out=line + "\n")
    stored_agents.ensure_checkout("connect-helper", run=run, home=tmp_path)
    assert calls[1:] == [["/stub/claude", "plugin", "marketplace", "update", "connect-helper"],
                         ["/stub/claude", "plugin", "update", "connect-helper@connect-helper",
                          "--scope", "user"]]


def test_is_stored_reads_the_claims_definition():
    assert stored_agents.is_stored({"definition": {"source": "stored"}})
    assert not stored_agents.is_stored({"definition": None})
    assert not stored_agents.is_stored({})
