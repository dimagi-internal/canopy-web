"""Project runs — a run of a kind of work on a project, DDD first.

Why: a DDD run's state lived only on the runner that started it. Another machine
could not resume it, two machines could mint the same run_id (canopy-web groups
walkthroughs and reviews by that string, so their packages would merge), and a
human's answer to a review reached only one disk.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.projects import services
from apps.projects.models import Project, ProjectContext, ProjectRun
from apps.projects.schemas import ProjectRunDetailOut, ProjectRunOut
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace

User = get_user_model()
pytestmark = pytest.mark.django_db


def _user(role=WorkspaceMembership.EDITOR, username="alice"):
    user = User.objects.create_user(username=username, email=f"{username}@dimagi.com", password="pw")
    ensure_member(a_workspace(), user, role)
    return user


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _project(slug="connect-labs"):
    return Project.objects.create(name=slug, slug=slug, workspace=a_workspace())


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


def _put(c, url, data):
    return c.put(url, data=json.dumps(data), content_type="application/json")


URL = "/api/projects/connect-labs/runs/"


class TestMint:
    def test_server_mints_the_run_id(self):
        _project()
        c = _client(_user())
        r = _post(c, URL, {"kind": "ddd", "subject": "supply-sophie", "holder": "runner-a"})
        assert r.status_code == 201, r.content
        body = ProjectRunDetailOut.model_validate(r.json())
        today = dt.datetime.now(dt.UTC).date().strftime("%Y-%m-%d")
        assert body.run_id == f"supply-sophie-{today}-001"
        assert body.status == "running" and body.holder == "runner-a"

        r2 = _post(c, URL, {"kind": "ddd", "subject": "supply-sophie"})
        assert r2.json()["run_id"] == f"supply-sophie-{today}-002"

    def test_mint_skips_ids_legacy_local_runs_already_uploaded(self):
        """A run minted on a runner's disk uploaded decks under its id; the server
        must not hand that id out again, or the two runs' packages merge."""
        from apps.walkthroughs.models import Walkthrough

        _project()
        today = dt.datetime.now(dt.UTC).date()
        legacy = f"supply-sophie-{today:%Y-%m-%d}-004"
        Walkthrough.objects.create(
            title="deck", kind="html", run_id=legacy, workspace=a_workspace(), owner=_user(),
            drive_file_id="f", drive_folder_id="d", content_type="text/html", size_bytes=1,
        )
        assert services.next_run_id("supply-sophie", today=today).endswith("-005")

    def test_adopt_an_existing_run_id_and_refuse_a_duplicate(self):
        _project()
        c = _client(_user())
        body = {"kind": "ddd", "subject": "s", "run_id": "s-2026-10-03-002", "state": {"iteration": 6}}
        r = _post(c, URL, body)
        assert r.status_code == 201 and r.json()["state_version"] == 1
        assert _post(c, URL, body).status_code == 409


class TestState:
    def _run(self, c):
        return _post(c, URL, {"kind": "ddd", "subject": "s", "holder": "runner-a"}).json()

    def test_round_trip_and_version_bump(self):
        _project()
        c = _client(_user())
        run = self._run(c)
        url = f"{URL}{run['run_id']}/state/"
        r = _put(c, url, {
            "state": {"iteration": 1, "findings": [{"scene": 1}]},
            "base_version": 0, "phase": "judged", "iteration": 1,
            "summary": {"objective": "product"}, "holder": "runner-a",
        })
        assert r.status_code == 200, r.content
        assert r.json()["state_version"] == 1 and r.json()["phase"] == "judged"
        got = c.get(f"{URL}{run['run_id']}/").json()
        assert got["state"]["findings"] == [{"scene": 1}]
        assert got["summary"] == {"objective": "product"}

    def test_a_stale_writer_gets_409_naming_the_other_holder(self):
        _project()
        c = _client(_user())
        run = self._run(c)
        url = f"{URL}{run['run_id']}/state/"
        assert _put(c, url, {"state": {"a": 1}, "base_version": 0, "holder": "runner-b"}).status_code == 200
        r = _put(c, url, {"state": {"a": 2}, "base_version": 0, "holder": "runner-a"})
        assert r.status_code == 409
        assert "runner-b" in r.json()["detail"]
        forced = _put(c, url, {"state": {"a": 2}, "base_version": 0, "force": True, "holder": "runner-a"})
        assert forced.status_code == 200 and forced.json()["state_version"] == 2

    def test_terminal_status_stamps_completed_and_active_filter(self):
        _project()
        c = _client(_user())
        done = self._run(c)
        live = self._run(c)
        _put(c, f"{URL}{done['run_id']}/state/", {"state": {}, "status": "converged_clean"})
        assert ProjectRun.objects.get(run_id=done["run_id"]).completed_at is not None
        active = c.get(f"{URL}?kind=ddd&active=true").json()
        assert [r["run_id"] for r in active] == [live["run_id"]]
        assert all("state" not in r for r in active)
        ProjectRunOut.model_validate(active[0])


class TestAccess:
    def test_viewer_reads_but_cannot_write(self):
        _project()
        editor = _client(_user())
        run = _post(editor, URL, {"kind": "ddd", "subject": "s"}).json()
        viewer = _client(_user(role=WorkspaceMembership.VIEWER, username="victor"))
        assert viewer.get(f"{URL}{run['run_id']}/").status_code == 200
        assert _post(viewer, URL, {"kind": "ddd", "subject": "s"}).status_code == 403
        assert _put(viewer, f"{URL}{run['run_id']}/state/", {"state": {}}).status_code == 403

    def test_non_member_cannot_see_runs(self):
        _project()
        outsider = User.objects.create_user(username="eve", email="eve@x.org", password="pw")
        assert _client(outsider).get(URL).status_code == 404

    def test_bearer_token_writes(self):
        from apps.tokens.models import PersonalToken

        _project()
        raw, _ = PersonalToken.create_for_user(user=_user(), label="runner")
        r = Client().post(
            URL, data=json.dumps({"kind": "ddd", "subject": "s"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        assert r.status_code == 201, r.content


class TestContextScope:
    def test_learnings_are_scoped_per_workflow(self):
        project = _project()
        c = _client(_user())
        base = "/api/projects/connect-labs/context/"
        entry = {"context_type": "learning", "content": "tomselect needs a hold", "source": "ddd", "scope": "ddd"}
        r = _post(c, base, entry)
        assert r.status_code == 201 and r.json()["scope"] == "ddd"
        ProjectContext.objects.create(project=project, context_type="note", content="general", source="x")
        ddd = c.get(f"{base}?scope=ddd&context_type=learning").json()
        assert [e["content"] for e in ddd] == ["tomselect needs a hold"]
        assert [e["content"] for e in c.get(f"{base}?scope=").json()] == ["general"]
        latest = c.get(f"{base}latest/?scope=ddd").json()["contexts"]
        assert set(latest) == {"learning"}
