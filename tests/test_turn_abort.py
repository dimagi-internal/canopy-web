"""A turn that cannot do its work ends FAILED and is raised as a failure — see
apps/harness/turn_abort.py. 2026-10-27: ACE turn 599fad3b failed its first Drive
call on credentials, emailed the outside person who asked about "runner logins",
and finished `done` with nothing alarmed."""
from __future__ import annotations

import uuid
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.attention.models import Alert
from apps.harness import auto_debug, services, turn_abort
from apps.harness.models import FailureInvestigation, Runner, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_user, a_workspace

pytestmark = pytest.mark.django_db

DRIVE_ERROR = ("Error: Method doesn't allow unregistered callers (callers without established "
               "identity). Please use API Key or other form of API consumer identity to call this API.")
TOOL = "mcp__plugin_ace_ace-gdrive__resolve_opp_path"


@pytest.fixture
def ws():
    return a_workspace()


@pytest.fixture
def ada(ws):
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=a_user())


@pytest.fixture
def ace(ws):
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, user=a_user("ace@dimagi-ai.com"))


@pytest.fixture
def runner():
    return Runner.objects.create(name="cloud-ec2-2", kind=Runner.EMDASH, owner=a_member())


def _running(agent, runner, status=Turn.RUNNING):
    return Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=f"k-{uuid.uuid4()}",
        status=status, claimed_by=runner, claimed_at=timezone.now(), session_key="s-1")


def _as(user):
    c = Client()
    c.force_login(user)
    return c


def _abort(client, turn, reason=DRIVE_ERROR, tool=TOOL):
    return client.post(f"/api/harness/turns/{turn.id}/abort", {"reason": reason, "tool": tool},
                       content_type="application/json")


def test_the_agent_aborts_its_own_running_turn_and_it_is_raised_as_a_failure(ada, ace, runner):
    turn = _running(ace, runner)
    with mock.patch("apps.harness.turn_abort.groups.publish") as publish:
        resp = _abort(_as(ace.user), turn)

    assert resp.status_code == 200, resp.content
    turn.refresh_from_db()
    assert turn.status == Turn.FAILED
    assert turn.result_note.startswith("Aborted by canopy: the turn could not do its work in "
                                       f"{TOOL}.")
    assert "unregistered callers" in turn.result_note
    # The existing failure path: an investigation, its alert, the debugger's turn.
    inv = FailureInvestigation.objects.get()
    assert inv.agents == ["ace"] and inv.runners == ["cloud-ec2-2"]
    assert Alert.objects.filter(subject_key=f"investigation:{inv.pk}").exists()
    assert Turn.objects.filter(idempotency_key__startswith=auto_debug.KEY_PREFIX).count() == 1
    # Said where the conversation is shown, and the box told to stop the session.
    assert turn.events.filter(kind="error").exists()
    status = turn.events.filter(kind="status").order_by("-seq").first()
    assert status.payload["aborted"] is True and status.payload["tool"] == TOOL
    publish.assert_any_call(mock.ANY, {"type": "runner.cancel", "turn_id": str(turn.id)})


def test_the_runners_later_finish_does_not_turn_it_back_to_done(ada, ace, runner):
    turn = _running(ace, runner)
    _abort(_as(ace.user), turn)
    services.finish_turn(Turn.objects.get(pk=turn.pk), status=Turn.DONE,
                         result_note="I emailed Sophie about the logins.")
    turn.refresh_from_db()
    assert turn.status == Turn.FAILED and turn.result_note.startswith("Aborted by canopy")


def test_a_laptop_turn_already_done_can_be_aborted(ada, ace, runner):
    """emdash finishes a laptop turn the moment its session exists; the work — and
    the failure — come after."""
    turn = _running(ace, runner, status=Turn.DONE)
    with mock.patch("apps.harness.turn_abort.groups.publish") as publish:
        assert _abort(_as(ace.user), turn).status_code == 200
    turn.refresh_from_db()
    assert turn.status == Turn.FAILED
    assert FailureInvestigation.objects.count() == 1
    publish.assert_not_called()  # nothing is running it to stop


def test_abort_is_idempotent(ada, ace, runner):
    turn = _running(ace, runner)
    client = _as(ace.user)
    assert _abort(client, turn).status_code == 200
    assert _abort(client, turn).status_code == 200
    assert FailureInvestigation.objects.get().occurrences == 1


def test_a_cancelled_turn_is_left_alone(ace, runner):
    turn = _running(ace, runner, status=Turn.CANCELLED)
    resp = _abort(_as(ace.user), turn)
    assert resp.status_code == 409
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_an_editor_of_the_agent_may_abort(ws, ace, runner):
    editor = a_user("editor@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    turn = _running(ace, runner)
    assert _abort(_as(editor), turn).status_code == 200


def test_another_agents_login_may_not(ws, ace, runner):
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=a_workspace("other"), user=a_user("eva@dimagi-ai.com"))
    turn = _running(ace, runner)
    assert _abort(_as(eva.user), turn).status_code == 404
    turn.refresh_from_db()
    assert turn.status == Turn.RUNNING


def test_the_note_is_bounded_and_flat():
    note = turn_abort.abort_note("line one\n\nline two " + "x" * 5000, "tool")
    assert "\n" not in note and len(note) < turn_abort.REASON_MAX + 100
