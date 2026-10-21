"""The runner writes its private-runner registration from the heartbeat (canopy-web#1705)."""
import json

from canopy_runner import private_runner

ME = {"id": "rid-1", "name": "jj-mbp-cdp", "host": "jj.local", "private": True,
      "private_runner_id": "pr-1"}


def test_a_private_runner_writes_the_registration(tmp_path):
    path = tmp_path / "private-runner.json"
    private_runner.observe(ME, base_url="https://canopy", path=path)
    doc = json.loads(path.read_text())
    assert (doc["id"], doc["runner_id"], doc["written_by"]) == ("pr-1", "rid-1", "canopy-runner")


def test_back_to_shared_removes_only_what_it_wrote(tmp_path):
    path = tmp_path / "private-runner.json"
    private_runner.observe(ME, base_url="https://canopy", path=path)
    private_runner.observe({**ME, "private": False}, base_url="x", path=path)
    assert not path.exists()


def test_a_persons_own_registration_is_left_alone(tmp_path):
    path = tmp_path / "private-runner.json"
    path.write_text(json.dumps({"id": "mine", "name": "aath-mbp"}))
    private_runner.observe(ME, base_url="x", path=path)
    private_runner.observe({**ME, "private": False}, base_url="x", path=path)
    assert json.loads(path.read_text())["id"] == "mine"


def test_an_old_server_changes_nothing(tmp_path):
    path = tmp_path / "private-runner.json"
    private_runner.observe({"id": "rid-1", "engine": "emdash"}, base_url="x", path=path)
    assert not path.exists()


def test_a_runner_puts_itself_in_a_lower_mode_from_its_environment(monkeypatch):
    import urllib.request

    from canopy_runner import client

    req = urllib.request.Request("https://canopy/api/harness/runners/r/claim")
    monkeypatch.delenv("CANOPY_ACTS_AS", raising=False)
    client._identify(req)
    assert req.get_header("X-canopy-acts-as") is None
    for raw, want in (("editor", "editor"), ("Viewer", "viewer"), ("admin", None)):
        monkeypatch.setenv("CANOPY_ACTS_AS", raw)
        r = urllib.request.Request("https://canopy/x")
        client._identify(r)
        assert r.get_header("X-canopy-acts-as") == want
