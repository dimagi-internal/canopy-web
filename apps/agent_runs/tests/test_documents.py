"""Run documents — a run of a kind of work on an agent's project (DDD first).

Why: a DDD run's state lived only on the runner that started it. Another machine
could not resume it, two machines could mint the same run id, and "which project
is this narrative for?" had no answer anywhere.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agent_runs.documents import RunDocDetailOut, RunDocOut, next_ext_id
from apps.agent_runs.models import AgentRun, AgentRunStep, AgentRunVerdict
from apps.agents.models import Agent, AgentProject
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
URL = "/api/agent-runs/"


@pytest.fixture
def ws():
    return a_workspace("labs-ws", access_request_domains=[])


def _user(ws, role=WorkspaceMembership.EDITOR, username="alice"):
    u = User.objects.create_user(username=username, email=f"{username}@dimagi.com", password="pw")
    WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
    return u


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


def _put(c, url, data):
    return c.put(url, data=json.dumps(data), content_type="application/json")


@pytest.fixture
def hal(ws):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws)


@pytest.fixture
def ace(ws):
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws)


@pytest.fixture
def sophie(hal):
    return AgentProject.objects.create(
        agent=hal, ext_id="P1", name="Sophie RUTF procurement", repo_slug="connect-labs",
        outcome="A procurement product Sophie would use",
    )


def _today():
    return timezone.now().date().strftime("%Y-%m-%d")


class TestMint:
    def test_server_mints_on_the_agents_project(self, ws, hal, sophie):
        c = _client(_user(ws))
        body = {"agent": "hal", "project": "P1", "kind": "ddd", "subject": "supply-sophie", "holder": "a@box"}
        r = _post(c, URL, body)
        assert r.status_code == 201, r.content
        body = RunDocDetailOut.model_validate(r.json())
        assert body.ext_id == f"supply-sophie-{_today()}-001"
        assert body.project.name == "Sophie RUTF procurement" and body.agent_slug == "hal"
        assert body.status == "running"
        r2 = _post(c, URL, {"agent": "hal", "kind": "ddd", "subject": "supply-sophie"})
        assert r2.json()["ext_id"].endswith("-002")

    def test_min_seq_keeps_clear_of_ids_minted_on_a_runners_disk(self, hal):
        assert next_ext_id("s", min_seq=4).endswith("-004")
        AgentRun.objects.create(agent=hal, kind="ddd", ext_id=f"s-{_today()}-007")
        assert next_ext_id("s", min_seq=4).endswith("-008")

    def test_adopt_and_refuse_duplicates(self, ws, hal):
        c = _client(_user(ws))
        body = {"agent": "hal", "kind": "ddd", "subject": "s", "ext_id": "s-2026-10-03-002", "state": {"iteration": 6}}
        r = _post(c, URL, body)
        assert r.status_code == 201 and r.json()["state_version"] == 1
        # The adopted run reads as in-progress through the lifecycle model.
        run = AgentRun.objects.get(ext_id="s-2026-10-03-002")
        assert run.steps.filter(status=AgentRunStep.RUNNING).values_list("key", flat=True)[0] == "iter-6"
        assert _post(c, URL, body).status_code == 409

    def test_project_must_belong_to_the_agent(self, ws, ace, sophie):
        c = _client(_user(ws))
        r = _post(c, URL, {"agent": "ace", "project": "P1", "kind": "ddd", "subject": "s"})
        assert r.status_code == 404


class TestState:
    def _mint(self, c):
        return _post(c, URL, {"agent": "hal", "project": "P1", "kind": "ddd", "subject": "s", "holder": "a@box"}).json()

    def test_round_trip_version_and_iteration_mirror(self, ws, hal, sophie):
        c = _client(_user(ws))
        run = self._mint(c)
        r = _put(c, f"{URL}{run['ext_id']}/state/", {
            "state": {"iteration": 2, "findings": [1]}, "base_version": 0, "iteration": 2, "score": 3.0,
            "summary": {"objective": "product", "progress": {"score": 3.0}}, "holder": "a@box",
        })
        assert r.status_code == 200, r.content
        assert r.json()["state_version"] == 1 and r.json()["current_step"] == "iter-2"
        got = c.get(f"{URL}{run['ext_id']}/").json()
        assert got["state"]["findings"] == [1]
        db = AgentRun.objects.get(ext_id=run["ext_id"])
        assert list(db.steps.order_by("ordinal").values_list("key", "status")) == [
            ("iter-0", "complete"), ("iter-1", "complete"), ("iter-2", "running"),
        ]
        assert AgentRunVerdict.objects.get(step__run=db, step__key="iter-2").score == 3.0

    def test_stale_writer_gets_409_naming_the_other_runner(self, ws, hal, sophie):
        c = _client(_user(ws))
        url = f"{URL}{self._mint(c)['ext_id']}/state/"
        assert _put(c, url, {"state": {}, "base_version": 0, "holder": "b@box"}).status_code == 200
        r = _put(c, url, {"state": {}, "base_version": 0, "holder": "a@box"})
        assert r.status_code == 409 and "b@box" in r.json()["detail"]
        assert _put(c, url, {"state": {}, "base_version": 0, "force": True}).status_code == 200

    def test_terminal_status_completes_the_lifecycle(self, ws, hal, sophie):
        c = _client(_user(ws))
        run = self._mint(c)
        _put(c, f"{URL}{run['ext_id']}/state/", {"state": {}, "status": "converged_clean", "iteration": 1})
        db = AgentRun.objects.get(ext_id=run["ext_id"])
        assert db.completed_at is not None
        assert set(db.steps.values_list("status", flat=True)) == {"complete"}


class TestDiscovery:
    def test_cross_agent_lookup_by_subject_and_repo(self, ws, hal, ace, sophie):
        c = _client(_user(ws))
        _post(c, URL, {"agent": "hal", "project": "P1", "kind": "ddd", "subject": "supply"})
        AgentProject.objects.create(agent=ace, ext_id="P1", name="Nutrition demo", repo_slug="connect-labs")
        _post(c, URL, {"agent": "ace", "project": "P1", "kind": "ddd", "subject": "nutrition"})
        found = c.get(f"{URL}?kind=ddd&subject=supply").json()
        assert [(r["agent_slug"], r["project"]["name"]) for r in found] == [("hal", "Sophie RUTF procurement")]
        RunDocOut.model_validate(found[0])
        assert "state" not in found[0]
        projects = c.get(f"{URL}projects/?repo_slug=connect-labs").json()
        assert {(p["agent_slug"], p["name"]) for p in projects} == {
            ("hal", "Sophie RUTF procurement"), ("ace", "Nutrition demo"),
        }

    def test_active_filter(self, ws, hal, sophie):
        c = _client(_user(ws))
        done = _post(c, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).json()
        live = _post(c, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).json()
        _put(c, f"{URL}{done['ext_id']}/state/", {"state": {}, "status": "stopped_not_converged"})
        assert [r["ext_id"] for r in c.get(f"{URL}?active=true").json()] == [live["ext_id"]]

    def test_plain_lifecycle_runs_are_not_documents(self, ws, hal):
        AgentRun.objects.create(agent=hal, label="ACE opp run")
        assert _client(_user(ws)).get(URL).json() == []


class TestAccess:
    def test_viewer_reads_not_writes(self, ws, hal, sophie):
        editor = _client(_user(ws))
        run = _post(editor, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).json()
        viewer = _client(_user(ws, role=WorkspaceMembership.VIEWER, username="vic"))
        assert viewer.get(f"{URL}{run['ext_id']}/").status_code == 200
        assert _post(viewer, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).status_code == 403
        assert _put(viewer, f"{URL}{run['ext_id']}/state/", {"state": {}}).status_code == 403

    def test_other_tenant_sees_nothing(self, ws, hal, sophie):
        editor = _client(_user(ws))
        run = _post(editor, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).json()
        other = a_workspace("other", access_request_domains=[])
        outsider = _client(_user(other, username="eve"))
        assert outsider.get(f"{URL}{run['ext_id']}/").status_code == 404
        assert outsider.get(URL).json() == []
        assert _post(outsider, URL, {"agent": "hal", "kind": "ddd", "subject": "s"}).status_code == 404

    def test_bearer_token(self, ws, hal):
        from apps.tokens.models import PersonalToken

        raw, _ = PersonalToken.create_for_user(user=_user(ws), label="runner")
        r = Client().post(
            URL, data=json.dumps({"agent": "hal", "kind": "ddd", "subject": "s"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        assert r.status_code == 201, r.content


def test_package_phase_comes_from_the_run_document(ws, hal, sophie):
    from apps.runs import aggregate

    run = AgentRun.objects.create(
        agent=hal, project=sophie, kind="ddd", ext_id="s-2026-10-04-001", status="running",
        summary={"phase": "judged", "iteration": 3},
    )
    pkg = aggregate.build_run(run.ext_id)
    assert pkg["phase"] == "judged · iteration 3"
    assert pkg["record"]["project_name"] == "Sophie RUTF procurement"
    AgentRun.objects.filter(pk=run.pk).update(status="converged_clean")
    assert aggregate.build_run(run.ext_id)["phase"] == "converged_clean"
    assert aggregate.build_run(run.ext_id, workspace_slugs={"elsewhere"}) is None


def test_the_state_lock_never_outer_joins():
    """Postgres rejects FOR UPDATE on the nullable side of an outer join, and
    ``project`` is nullable. SQLite (the test DB) ignores FOR UPDATE, so assert
    the SQL shape instead — this 500'd every state write in production."""
    from apps.agent_runs.documents import _locked

    sql = str(_locked(1).query).upper()
    assert "OUTER JOIN" not in sql and "JOIN" not in sql
