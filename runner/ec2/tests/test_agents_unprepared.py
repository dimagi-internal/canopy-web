"""The box reports the agents it tried to set up and could not, so canopy-web
stops handing it their turns (routing.cannot_serve; fizzy on cloud-ec2-2,
2026-10-15: a turn ran in a bare scratch dir with no plugin and no credentials)."""
from __future__ import annotations


def _clone(root, slug):
    (root / slug / ".git").mkdir(parents=True)


def test_nothing_tried_is_unknown_not_all_fine(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    assert cloud_runner.agents_unprepared() is None
    assert "agents_unprepared" not in cloud_runner._heartbeat_body([])


def test_bootstrap_failures_are_reported_until_a_clone_appears(cloud_runner, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    (tmp_path / ".unprepared").write_text("fizzy\nhal\n")
    _clone(tmp_path, "hal")  # a later lazy prepare got it
    assert cloud_runner.agents_unprepared() == ["fizzy"]
    assert cloud_runner._heartbeat_body([])["agents_unprepared"] == ["fizzy"]
    (tmp_path / ".unprepared").write_text("")
    assert cloud_runner.agents_unprepared() == []


def test_a_failed_lazy_prepare_is_reported(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    cloud_runner._ensure_agent_clone("fizzy")  # the test bootstrapper clones nothing
    assert cloud_runner.agents_unprepared() == ["fizzy"]
    monkeypatch.setattr(cloud_runner, "_LAZY_BOOTSTRAPPER", lambda s: _clone(tmp_path, s))
    cloud_runner._LAZY_BOOTSTRAP_TRIED.clear()
    cloud_runner._ensure_agent_clone("eva")
    assert cloud_runner.agents_unprepared() == []
