"""Engagement events — what people actually do with projects and huddles
(spec 2026-10-16-work-group-huddles-design.md §7).

Pinned here:

* `engagement.record` validates kind and surface (a typo raises), takes a
  workspace or its slug, and never lets a database failure reach its caller;
* `dedupe_day` counts the same act by the same person on the same object once
  per UTC day, and a new day counts again;
* the view beacon records a project or a huddle the caller can see, once a day,
  and 404s on anything they cannot — a guess confirms nothing.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import DatabaseError

from apps.agents import engagement
from apps.agents.models import AgentProject, EngagementEvent
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_people_brain import _agent, _client, _member

pytestmark = pytest.mark.django_db

VIEW = "/api/engagement/view"


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    viewer = _member(ws, "sophie", role=WorkspaceMembership.VIEWER)
    ada = _agent(ws, "ada", owner)
    project = AgentProject.objects.create(agent=ada, ext_id="P1", name="Prompt library")
    Turn.objects.create(agent=ada, idempotency_key="anchor", cli_session_id="huddle:h1",
                        origin_ref={"kind": "huddle", "huddle": "h1", "leader": "ada"})
    return {"owner": owner, "ws": ws, "viewer": viewer, "ada": ada, "project": project}


def test_record_takes_a_workspace_or_its_slug_and_returns_the_row(world):
    a = engagement.record("backed", workspace=world["ws"], user=world["viewer"],
                          project=world["project"], meta={"stance": "backs"})
    b = engagement.record("run_announced", workspace="connect", surface="slack", group="champions-a")
    assert a.workspace_id == b.workspace_id == "connect"
    assert (a.kind, a.surface, a.meta) == ("backed", "web", {"stance": "backs"})
    assert (b.user, b.group) == (None, "champions-a")


@pytest.mark.parametrize("kw", [{"kind": "liked"}, {"kind": "viewed", "surface": "fax"}])
def test_an_unknown_kind_or_surface_is_a_programming_error(world, kw):
    with pytest.raises(ValueError):
        engagement.record(kw.pop("kind"), workspace="connect", **kw)
    assert not EngagementEvent.objects.exists()


def test_a_database_failure_never_reaches_the_caller(world):
    with mock.patch.object(EngagementEvent.objects, "create", side_effect=DatabaseError("down")):
        assert engagement.record("viewed", workspace="connect", user=world["viewer"]) is None


def test_dedupe_day_counts_one_act_per_person_per_object_per_utc_day(world):
    rec = lambda **kw: engagement.record("viewed", workspace="connect", dedupe_day=True, **kw)  # noqa: E731
    assert rec(user=world["viewer"], project=world["project"]) is not None
    assert rec(user=world["viewer"], project=world["project"]) is None
    assert rec(user=world["owner"], project=world["project"]) is not None  # another person
    assert rec(user=world["viewer"], huddle_id="h1") is not None  # another object
    # Yesterday's view does not hold today's back.
    EngagementEvent.objects.update(at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert rec(user=world["viewer"], project=world["project"]) is not None


def test_the_beacon_records_a_project_view_once_a_day(world):
    c = _client(world["viewer"])
    body = {"project": world["project"].pk}
    assert c.post(VIEW, body, content_type="application/json").json() == {"recorded": True}
    assert c.post(VIEW, body, content_type="application/json").json() == {"recorded": False}
    [ev] = EngagementEvent.objects.all()
    assert (ev.kind, ev.surface, ev.user, ev.project, ev.workspace_id) == (
        "viewed", "web", world["viewer"], world["project"], "connect")


def test_the_beacon_records_a_huddle_view(world):
    r = _client(world["viewer"]).post(VIEW, {"huddle_id": "h1"}, content_type="application/json")
    assert r.json() == {"recorded": True}
    assert EngagementEvent.objects.get().huddle_id == "h1"


def test_the_beacon_404s_what_the_caller_cannot_see(world):
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    c = _client(stranger)
    assert c.post(VIEW, {"project": world["project"].pk}, content_type="application/json").status_code == 404
    assert c.post(VIEW, {"huddle_id": "h1"}, content_type="application/json").status_code == 404
    assert _client(world["viewer"]).post(VIEW, {"huddle_id": "nope"},
                                         content_type="application/json").status_code == 404
    assert not EngagementEvent.objects.exists()


@pytest.mark.parametrize("body", [{}, {"project": 1, "huddle_id": "h1"}])
def test_the_beacon_names_exactly_one_object(world, body):
    r = _client(world["viewer"]).post(VIEW, body, content_type="application/json")
    assert r.status_code == 422
