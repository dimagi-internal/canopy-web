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


def test_a_new_agent_is_cloned_with_its_turns_github_identity(cloud_runner, tmp_path, monkeypatch):
    """canopy-web never hands the agent's GitHub token to a read outside a turn, so the
    box-wide prepare cannot clone a private agent repo: the first turn clones it with
    ITS token, then prepare runs."""
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    seen = []
    monkeypatch.setattr(cloud_runner, "_LAZY_CLONER", lambda s, env: seen.append((s, dict(env or {}))))
    env = {"GH_TOKEN": "t", cloud_runner.AGENT_REPO_ENV: "dimagi-internal/fizzy"}
    cloud_runner._ensure_agent_clone("fizzy", env)
    assert seen == [("fizzy", env)]


def test_the_cloner_needs_the_turns_token_and_a_plain_repo(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    ran = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    cloud_runner._clone_agent_repo("fizzy", {cloud_runner.AGENT_REPO_ENV: "dimagi-internal/fizzy"})
    cloud_runner._clone_agent_repo("fizzy", {"GH_TOKEN": "t", cloud_runner.AGENT_REPO_ENV: "../evil"})
    assert ran == []


def test_an_agentless_turn_never_inherits_the_boxs_canopy_token(cloud_runner, monkeypatch):
    """A repo chat routed to this box may be anyone's; it reaches canopy as its asker
    (caller token), never as whoever paired the box."""
    monkeypatch.setenv("CANOPY_TOKEN", "box-owners-pat")
    assert "CANOPY_TOKEN" not in cloud_runner._agent_env(None)
    assert "CANOPY_TOKEN" not in cloud_runner._agent_env("")
