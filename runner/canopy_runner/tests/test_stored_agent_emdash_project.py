"""A stored agent on a laptop with emdash: its materialized checkout is its emdash project,
so emdash drives its turns, a caller's confined turn included, as it drives a repo agent's."""
import json

import pytest

from canopy_runner import caller, cdp_control, emdash, execute, stored_agents
from tests.test_execute import FakeClient, _cfg
from tests.test_restricted_execution import CAP

SLUG = "connect-helper"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    return tmp_path


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A laptop with emdash: a checkout materializes, and emdash's projects live in a dict
    the faked Add Project dialog writes to."""
    db = tmp_path / "emdash4.db"
    db.write_text("")
    cfg = _cfg()
    cfg.emdash_db = str(db)
    dest = tmp_path / ".canopy" / "agents" / SLUG
    dest.mkdir(parents=True)
    projects: dict = {}
    added: list = []
    monkeypatch.setattr(stored_agents, "ensure_checkout", lambda slug, **kw: dest)
    monkeypatch.setattr(emdash, "project_path", lambda db_path, name: projects.get(name))
    monkeypatch.setattr(emdash, "list_projects", lambda db_path: sorted(projects))

    def add(name, path, port=9222):
        added.append((name, path))
        projects[name] = path
        return {"action": "submitted"}

    monkeypatch.setattr(cdp_control, "add_project", add)
    return cfg, dest, projects, added


def _confined_stored(**kw):
    d = {"id": "3f2b8c1e-0000-4000-8000-000000000002", "agent_slug": SLUG,
         "definition": {"source": "stored", "revision": 3},
         "origin_ref": {"thread_id": "18c9abc"}, "prompt": "hello", "mcp_token": "cct_test",
         "caller_context": {"profile": "confined", "capability": CAP,
                            "conversation": {"thread_id": "18c9abc"}}}
    d.update(kw)
    return d


def test_a_confined_stored_agent_turn_runs_through_emdash_with_its_confinement(
        monkeypatch, _isolated, box):
    cfg, dest, _projects, added = box
    seen = {}

    def create(agent, prompt, task_name, port):
        seen["profile_at_create"] = json.loads((_isolated / "profiles" / f"{task_name}.json").read_text())
        seen.update(agent=agent, task=task_name)
        return {"task": task_name}

    monkeypatch.setattr(cdp_control, "create_task", create)
    client = FakeClient({"reuse": False})
    action = execute.execute_turn(cfg, client, "r-1", _confined_stored())
    assert not client.failed, client.failed
    assert action.startswith("created:")
    assert added == [(SLUG, str(dest))]
    assert seen["agent"] == SLUG and seen["task"].startswith("cx-")
    assert seen["profile_at_create"]["capability"]["name"] == "ask"


def test_the_project_is_added_once_and_points_at_the_materialized_checkout(box):
    cfg, dest, projects, added = box
    assert stored_agents.ensure_emdash_project(cfg, SLUG) == ""
    assert stored_agents.ensure_emdash_project(cfg, SLUG) == ""
    assert added == [(SLUG, str(dest))]
    assert projects[SLUG] == str(dest)


def test_a_project_of_that_name_at_another_folder_is_refused_not_driven(box, tmp_path):
    cfg, _dest, projects, added = box
    projects[SLUG] = str(tmp_path / "elsewhere")
    said = stored_agents.ensure_emdash_project(cfg, SLUG)
    assert "not the stored agent's checkout" in said
    assert added == []


def test_a_busy_emdash_says_so(box, monkeypatch):
    cfg, *_ = box

    def busy(*a, **k):
        raise cdp_control.CDPError("BUSY: a dialog is open")

    monkeypatch.setattr(cdp_control, "add_project", busy)
    assert "dialog open" in stored_agents.ensure_emdash_project(cfg, SLUG)


def test_a_dialog_that_closed_without_a_project_is_not_a_project(box, monkeypatch):
    cfg, *_ = box
    monkeypatch.setattr(cdp_control, "add_project", lambda *a, **k: {"action": "submitted"})
    clock = iter(range(0, 100, 20))
    said = stored_agents.ensure_emdash_project(cfg, SLUG, sleep=lambda s: None, now=lambda: next(clock))
    assert "no project" in said


def test_a_checkout_that_cannot_be_fetched_fails_with_the_reason(box, monkeypatch):
    cfg, *_ = box

    def broken(slug, **kw):
        raise stored_agents.StoredAgentError("could not fetch stored agent connect-helper: boom")

    monkeypatch.setattr(stored_agents, "ensure_checkout", broken)
    assert "boom" in stored_agents.ensure_emdash_project(cfg, SLUG)


def test_a_repo_agents_confined_turn_is_unchanged(monkeypatch, _isolated, box):
    cfg, _dest, projects, added = box
    projects["ace"] = "/repos/ace"
    monkeypatch.setattr(stored_agents, "ensure_checkout",
                        lambda *a, **k: pytest.fail("a repo agent has no stored checkout"))
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: {"task": task_name})
    client = FakeClient({"reuse": False})
    turn = _confined_stored(agent_slug="ace", definition={"source": "repo"})
    assert execute.execute_turn(cfg, client, "r-1", turn).startswith("created:")
    assert not client.failed and added == []


def test_every_heartbeat_reports_it(monkeypatch):
    from canopy_runner.client import Client

    sent = {}
    c = Client("http://x", "t")
    monkeypatch.setattr(c, "_call", lambda method, path, body: sent.update(body) or (200, {}))
    c.heartbeat("r-1", [])
    assert sent["stored_projects"] == stored_agents.STORED_PROJECTS_VERSION == 1
