"""The cloud runner moves off an address canopy has left (cloud_runner.maybe_rebase)."""
import io
import json

OLD = "https://labs.connect.dimagi.com/canopy"
NEW = "https://canopy.dimagi.com"


def _setup(cloud_runner, monkeypatch, tmp_path, *, base=OLD, rows=({"id": "r-1"},), in_flight=()):
    env = tmp_path / "runner.env"
    env.write_text(f"CANOPY_BASE_URL={base}\nCANOPY_TOKEN=t\n")
    monkeypatch.setattr(cloud_runner, "RUNNER_ENV_FILE", str(env))
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
    assert f"CANOPY_BASE_URL={NEW}" in env.read_text()
    assert "CANOPY_TOKEN=t" in env.read_text()
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
    assert f"CANOPY_BASE_URL={OLD}" in env.read_text() and not restarts
