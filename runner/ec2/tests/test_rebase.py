"""The cloud runner moves off an address canopy has left (cloud_runner.maybe_rebase)."""
import io
import json

OLD = "https://labs.connect.dimagi.com/canopy"
NEW = "https://canopy.dimagi.com"


def _setup(cloud_runner, monkeypatch, tmp_path, *, base=OLD, rows=({"id": "r-1"},), in_flight=()):
    env = tmp_path / "base_url.override"
    monkeypatch.setattr(cloud_runner, "BASE_URL_OVERRIDE_FILE", str(env))
    monkeypatch.setattr(cloud_runner, "BASE_URL", base)
    monkeypatch.setattr(cloud_runner, "_in_flight_ids", lambda: list(in_flight))
    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(list(rows)).encode()))
    restarts = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda *a, **k: restarts.append(a))
    return env, restarts


def test_moves_and_restarts(cloud_runner, monkeypatch, tmp_path):
    env, restarts = _setup(cloud_runner, monkeypatch, tmp_path)
    assert cloud_runner.maybe_rebase(NEW, "r-1")
    assert env.read_text().strip() == NEW
    assert restarts


def test_stays_unless_every_guard_holds(cloud_runner, monkeypatch, tmp_path):
    env, restarts = _setup(cloud_runner, monkeypatch, tmp_path, base="https://elsewhere.example")
    assert not cloud_runner.maybe_rebase(NEW, "r-1")
    env, restarts = _setup(cloud_runner, monkeypatch, tmp_path)
    assert not cloud_runner.maybe_rebase("http://canopy.dimagi.com", "r-1")
    env, restarts = _setup(cloud_runner, monkeypatch, tmp_path, rows=({"id": "other"},))
    assert not cloud_runner.maybe_rebase(NEW, "r-1")
    env, restarts = _setup(cloud_runner, monkeypatch, tmp_path, in_flight=("t-1",))
    assert not cloud_runner.maybe_rebase(NEW, "r-1")
    assert not env.exists() and not restarts


def test_a_moved_box_starts_on_its_new_address_whatever_runner_env_says(cloud_runner, monkeypatch, tmp_path):
    """runner.env is re-rendered from the stack parameter on every start; the
    override file is what survives, so the move does not undo itself."""
    override = tmp_path / "base_url.override"
    override.write_text(NEW + "\n")
    monkeypatch.setattr(cloud_runner, "BASE_URL_OVERRIDE_FILE", str(override))
    monkeypatch.setenv("CANOPY_BASE_URL", OLD)
    monkeypatch.setenv("CANOPY_WEB_API_URL", OLD)
    assert cloud_runner._initial_base_url() == NEW
    import os
    assert os.environ["CANOPY_BASE_URL"] == NEW          # bootstrap_agents.sh reads this
    assert os.environ["CANOPY_WEB_API_URL"] == NEW       # the canopy CLI reads this


def test_no_override_means_the_env_address(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "BASE_URL_OVERRIDE_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("CANOPY_BASE_URL", NEW)
    assert cloud_runner._initial_base_url() == NEW


def test_an_agent_env_naming_the_old_address_is_given_the_current_one(cloud_runner, monkeypatch, tmp_path):
    """An agent .env provisioned before the move names the old address, and is
    layered over the turn's env — so every CLI call in that agent's turns went
    to the old address after the box itself had moved (2026-10-06)."""
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "BASE_URL", NEW)
    (tmp_path / ".ace").mkdir()
    (tmp_path / ".ace" / ".env").write_text(
        f"CANOPY_WEB_API_URL={OLD}\nCANOPY_WEB_PAT=ace-pat\nOTHER_URL=https://elsewhere.example\n")
    env = cloud_runner._agent_env("ace")
    assert env["CANOPY_WEB_API_URL"] == NEW
    assert env["CANOPY_WEB_PAT"] == "ace-pat"
    assert env["OTHER_URL"] == "https://elsewhere.example"     # only canopy's address is rewritten
