"""A turn names the canopy conversation it ran in.

`canopy agent dispatch` hands back only a turn id; following up in the same
conversation (`POST /api/canopy-sessions/<id>/send`) needs the canopy Session's
id, which `TurnOut.session_id` is not (that is the runner's own name for its live
session). `Turn.canopy_session` is stamped when the runner's record-session makes
the session, and the turn read/list serve it as `canopy_session_id` + its page.
"""
from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _base(settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example/"


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="D", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="", owner=owner,
                                workspace=ws, status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now())
    c = Client()
    c.force_login(owner)
    return {"owner": owner, "ws": ws, "ada": ada, "box": box, "client": c}


def _turn(w, key="d1", **kw):
    fields = {"claimed_by": w["box"], "status": Turn.RUNNING, "enqueued_by": w["owner"], **kw}
    return Turn.objects.create(agent=w["ada"], origin=Turn.ORIGIN_API, idempotency_key=key,
                               prompt="do the thing", **fields)


def _record(w, turn, *, session_key="cli-1", send_turn_id=True, thread_key=None):
    body = {"thread_key": thread_key or f"ada:{turn.pk}", "session_key": session_key,
            "session_id": session_key, "agent_slug": "ada"}
    if send_turn_id:
        body["turn_id"] = str(turn.pk)
    r = w["client"].post(f"/api/harness/runners/{w['box'].pk}/record-session", body,
                         content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(runner_binding__session_key=session_key)


def _get(w, turn):
    r = w["client"].get(f"/api/harness/turns/{turn.pk}")
    assert r.status_code == 200, r.content
    return r.json()


def test_a_cloud_turn_names_the_session_its_runner_made(world):
    turn = _turn(world)
    session = _record(world, turn)
    body = _get(world, turn)
    assert body["canopy_session_id"] == str(session.pk)
    assert body["canopy_session_url"] == f"https://canopy.example/w/dimagi/sessions/{session.pk}"
    # Not the runner's live-session hint, which is a different thing.
    assert body["session_id"] != body["canopy_session_id"]


def test_a_laptop_turn_is_linked_by_its_agent_thread_key(world):
    # The laptop runner records `<agent>:<turn id>` and sends no turn_id.
    turn = _turn(world)
    session = _record(world, turn, send_turn_id=False)
    assert _get(world, turn)["canopy_session_id"] == str(session.pk)


def test_another_runners_thread_key_links_nothing(world):
    other = Runner.objects.create(name="laptop", kind=Runner.CLOUD, host="", owner=world["owner"],
                                  workspace=world["ws"], status=Runner.ONLINE,
                                  last_heartbeat_at=timezone.now())
    turn = _turn(world, claimed_by=other)
    _record(world, turn, send_turn_id=False)
    turn.refresh_from_db()
    assert turn.canopy_session_id is None


def test_the_first_session_wins(world):
    turn = _turn(world)
    first = _record(world, turn)
    _record(world, turn, session_key="cli-2", thread_key="ada:elsewhere")
    turn.refresh_from_db()
    assert turn.canopy_session_id == first.pk


def test_a_queued_turn_has_no_session_yet(world):
    turn = _turn(world, claimed_by=None, status=Turn.QUEUED)
    body = _get(world, turn)
    assert body["canopy_session_id"] is None
    assert body["canopy_session_url"] is None


def test_a_chat_turn_names_its_own_chat(world):
    chat = Session.objects.create(workspace=world["ws"], agent=world["ada"],
                                  created_by=world["owner"], title="chat")
    turn = Turn.objects.create(chat_session=chat, origin=Turn.ORIGIN_API, idempotency_key="c1",
                               prompt="hi", enqueued_by=world["owner"])
    body = _get(world, turn)
    assert body["canopy_session_id"] == str(chat.pk)
    assert body["canopy_session_url"] == f"https://canopy.example/w/dimagi/sessions/{chat.pk}"


def test_the_list_reads_the_sessions_without_a_query_per_turn(world):
    def list_queries():
        with CaptureQueriesContext(connection) as ctx:
            r = world["client"].get("/api/harness/turns/?agent=ada")
        assert r.status_code == 200, r.content
        return r.json(), len(ctx.captured_queries)

    def ran(key):  # one executing turn per agent: finish each before the next
        turn = _turn(world, key=key, enqueued_by=None)
        _record(world, turn, session_key=f"s-{key}")
        Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)

    ran("a")
    rows, one = list_queries()
    assert rows[0]["canopy_session_url"]
    for key in ("b", "c", "d"):
        ran(key)
    rows, four = list_queries()
    assert all(r["canopy_session_id"] for r in rows)
    assert four == one


def test_the_migration_links_turns_recorded_before_the_column(world):
    turn = _turn(world)
    session = _record(world, turn)
    Turn.objects.filter(pk=turn.pk).update(canopy_session=None)
    RunnerBinding.objects.create(session=Session.objects.create(workspace=world["ws"],
                                                                agent=world["ada"]),
                                 thread_key="ada:not-a-uuid")
    mod = importlib.import_module("apps.harness.migrations.0112_turn_canopy_session")
    mod.link_existing_agent_turns(django_apps, None)
    turn.refresh_from_db()
    assert turn.canopy_session_id == session.pk
