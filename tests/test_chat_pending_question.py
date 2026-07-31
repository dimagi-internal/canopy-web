"""The dialog a blocked agent is waiting on, from report to phone.

The bug this closes: on 2026-07-31 `ace`'s `spark` session blocked on an
`AskUserQuestion` for 52 minutes. The menu reached nothing — it was produced
only as a view-only live frame (never persisted, never in the connect
snapshot), and only ever by a chat send failing against it, so opening the
session showed a chat that had simply stopped. See
`canopy_transcript.questions` for why the transcript rather than the screen is
now the source.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

MENU = {
    "question": "How should the run proceed?",
    "title": "Phase 3→4",
    "body": "",
    "selected": None,
    "options": [
        {"number": 1, "label": "Proceed to Phase 4", "description": "carry on"},
        {"number": 2, "label": "Stop the run here", "description": "end here"},
    ],
    "source": "transcript",
}


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, ws):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def _report(client, runner_id, sessions):
    return client.post(
        f"/api/harness/runners/{runner_id}/sessions",
        {"sessions": sessions},
        content_type="application/json",
    )


def _setup():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    client = Client()
    client.force_login(jj)
    return jj, ws, runner, client


# -- the report writes it through ------------------------------------------


def test_a_reported_question_lands_on_the_binding():
    _jj, _ws, runner, client = _setup()
    assert _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
    ]).status_code == 200
    binding = RunnerBinding.objects.get(session_key="spark")
    assert binding.pending_question["question"] == "How should the run proceed?"
    assert [o["number"] for o in binding.pending_question["options"]] == [1, 2]


def test_answering_at_the_laptop_clears_it_on_the_next_report():
    """The report is a fresh observation every ~10s, so "no dialog" has to be
    able to retire one. Otherwise a menu answered at the keyboard keeps live
    buttons on every phone, and a tap types a number at what is now a prompt."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": None}])
    assert RunnerBinding.objects.get(session_key="spark").pending_question is None


def test_a_runner_that_never_heard_of_this_still_reports_normally():
    """An old runner omits the field. That lands as "no dialog", which is the
    safe direction — a phone with no buttons, never buttons against a dialog
    that is not there."""
    _jj, _ws, runner, client = _setup()
    assert _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace"},
    ]).status_code == 200
    assert RunnerBinding.objects.get(session_key="spark").pending_question is None


# -- the phone can see it --------------------------------------------------


def test_the_session_list_flags_a_session_waiting_on_a_human():
    """"The session stopped" was the whole symptom. A list that cannot say
    "this one is waiting on you" leaves a blocked agent looking identical to an
    idle one."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
        {"emdash_task": "quiet", "project": "ace", "question": None},
    ])
    rows = {r["title"]: r for r in client.get("/api/canopy-sessions/").json()}
    assert rows["spark"]["waiting_on_you"] is True
    assert rows["quiet"]["waiting_on_you"] is False


def test_opening_the_session_returns_the_menu():
    """The fix for "when I click on the session I don't see the menu". The live
    `session.activity` frame is view-only and exists only for a client that was
    already connected when it fired; the menu has to survive being looked at
    later."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    session = Session.objects.get(runner_binding__session_key="spark")
    body = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert body["menu"]["question"] == "How should the run proceed?"
    assert body["menu"]["options"][0]["label"] == "Proceed to Phase 4"


def test_a_session_with_no_dialog_carries_no_menu():
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "quiet", "project": "ace"}])
    session = Session.objects.get(runner_binding__session_key="quiet")
    assert client.get(f"/api/canopy-sessions/{session.id}").json()["menu"] is None


def test_an_unbound_session_has_no_menu_and_does_not_explode():
    """A web chat that has never been sent has no binding at all — no box, no
    screen, nothing to be blocked on."""
    jj, ws, _runner, client = _setup()
    session = Session.objects.create(workspace=ws, title="fresh", created_by=jj)
    body = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert body["menu"] is None
    assert body["waiting_on_you"] is False


# -- the WS snapshot, which is what ChatPage actually reads ------------------


def test_the_connect_snapshot_carries_the_menu():
    """`ChatPage` renders `socket.state.menu`, and the reducer only ever learned
    it from a live frame. Seeding it from the snapshot is what makes a menu
    survive a reconnect — or a phone that was never connected when it fired."""
    from apps.canopy_sessions import serializers

    jj, ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    session = Session.objects.select_related("runner_binding").get(
        runner_binding__session_key="spark")
    dto = serializers.session_state_dto(
        session=session, current_user_id=jj.id, participants=[], present_ids=[],
        draft=None, messages=[],
    )
    assert dto["menu"]["question"] == "How should the run proceed?"


def test_the_snapshot_shape_is_stable_when_nothing_is_pending():
    """The field is always present. A client that has to distinguish "absent"
    from "null" grows a second code path for the same fact."""
    from apps.canopy_sessions import serializers

    jj, ws, _runner, _client = _setup()
    session = Session.objects.create(workspace=ws, title="fresh", created_by=jj)
    dto = serializers.session_state_dto(
        session=session, current_user_id=jj.id, participants=[], present_ids=[],
        draft=None, messages=[],
    )
    assert "menu" in dto and dto["menu"] is None
