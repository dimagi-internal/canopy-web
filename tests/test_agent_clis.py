"""Agents declare which CLIs they run on; runner credentials name theirs
(spec 2026-10-26-multi-cli-agents §1–2)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import runners as harness_runners
from apps.harness.models import Runner, RunnerLogin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)
    return owner, editor, ws, agent


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _upsert(c, **fields):
    return c.post("/api/w/canopy/agents/", {"slug": "echo", "name": "Echo", **fields},
                  content_type="application/json")


def test_upsert_sets_clis_and_models(world):
    owner, _e, _ws, agent = world
    r = _upsert(_as(owner), clis=["codex", "claude-code"], cli_models={"codex": "gpt-6.1-sol"})
    assert r.status_code == 201, r.content
    agent.refresh_from_db()
    assert agent.clis == ["codex", "claude-code"]
    assert agent.cli_models == {"codex": "gpt-6.1-sol"}
    detail = _as(owner).get("/api/w/canopy/agents/echo/").json()
    assert detail["clis"] == ["codex", "claude-code"]
    assert detail["cli_models"] == {"codex": "gpt-6.1-sol"}
    runtime = _as(owner).get("/api/w/canopy/agents/echo/runtime").json()
    assert runtime["clis"] == ["codex", "claude-code"]


def test_upsert_without_clis_keeps_them(world):
    owner, _e, _ws, agent = world
    Agent.objects.filter(pk=agent.pk).update(clis=["codex"])
    assert _upsert(_as(owner), description="x").status_code == 201
    agent.refresh_from_db()
    assert agent.clis == ["codex"]


@pytest.mark.parametrize("value", [[], ["codex", "codex"], ["gpt"]])
def test_bad_clis_refused(world, value):
    owner, *_ = world
    assert _upsert(_as(owner), clis=value).status_code == 422


def test_bad_cli_model_key_refused(world):
    owner, *_ = world
    assert _upsert(_as(owner), cli_models={"gpt": "x"}).status_code == 422


def test_non_admin_cannot_change_clis(world):
    _o, editor, _ws, agent = world
    r = _upsert(_as(editor), clis=["codex"])
    assert r.status_code == 403
    assert "clis" in r.json()["detail"]
    agent.refresh_from_db()
    assert agent.clis == ["claude-code"]


def test_admin_sets_clis_by_patch(world):
    owner, editor, ws, agent = world
    # Codex can be added once a runner serving the workspace has it.
    Runner.objects.create(name="box", kind=Runner.CLOUD, owner=owner, workspace_id=ws.pk,
                          last_heartbeat_at=timezone.now(),
                          capabilities={"clis": {"claude-code": {"signed_in": True},
                                                 "codex": {"signed_in": True}}})
    url = "/api/w/canopy/agents/echo/clis"
    body = {"clis": ["codex", "claude-code"], "cli_models": {"codex": "gpt-6.1-sol"}}
    assert _as(editor).patch(url, body, content_type="application/json").status_code == 403
    r = _as(owner).patch(url, body, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["clis"] == ["codex", "claude-code"]
    agent.refresh_from_db()
    assert agent.cli_models == {"codex": "gpt-6.1-sol"}


# ---- runner credentials carry their CLI ----
def _runner(owner):
    return Runner.objects.create(name="box", kind=Runner.CLOUD, owner=owner,
                                 last_heartbeat_at=timezone.now())


def test_cascade_items_carry_cli(world):
    owner, *_ = world
    runner = _runner(owner)
    RunnerLogin.objects.create(runner=runner, key="default", kind=RunnerLogin.LOGIN)
    RunnerLogin.objects.create(runner=runner, key="aaaa0001", kind=RunnerLogin.API_KEY,
                               cli="codex", position=1)
    # Code that reports no CLIs reads every entry as Claude's: it never sees Codex's.
    bundle = harness_runners.get_runner_credential(runner)
    assert [(c["key"], c["cli"]) for c in bundle["cascade"]] == [("default", "claude-code")]
    runner.capabilities = {"clis": {"claude-code": {"signed_in": True}, "codex": {"signed_in": False}}}
    runner.save(update_fields=["capabilities"])
    bundle = harness_runners.get_runner_credential(runner)
    assert [(c["key"], c["cli"]) for c in bundle["cascade"]] == [
        ("default", "claude-code"), ("aaaa0001", "codex")]


def test_last_fallback_is_per_cli(world, monkeypatch):
    owner, *_ = world
    runner = _runner(owner)
    RunnerLogin.objects.create(runner=runner, key="aaaa0001", kind=RunnerLogin.TOKEN, position=0)
    RunnerLogin.objects.create(runner=runner, key="aaaa0002", kind=RunnerLogin.API_KEY,
                               cli="codex", position=1)
    monkeypatch.setattr(harness_runners, "_vault_secret_names",
                        lambda r: {"claude-token:aaaa0001", "openai-api-key:aaaa0002"})
    with pytest.raises(harness_runners.CredentialRefused):
        harness_runners.remove_runner_credential(runner, "aaaa0002")
    with pytest.raises(harness_runners.CredentialRefused):
        harness_runners.remove_runner_credential(runner, "aaaa0001")


def test_codex_token_kind_refused(world, monkeypatch):
    owner, *_ = world
    runner = _runner(owner)
    monkeypatch.setattr(harness_runners, "open_runner_vault", lambda r: object())
    with pytest.raises(harness_runners.CredentialRefused):
        harness_runners.add_runner_credential(runner, kind=RunnerLogin.TOKEN, cli="codex")
    entry = harness_runners.add_runner_credential(runner, kind=RunnerLogin.API_KEY, cli="codex")
    assert entry.cli == "codex"
