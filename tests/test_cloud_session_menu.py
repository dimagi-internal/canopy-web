"""A cloud runner's blocked agent reaches the same menu surface a laptop's does.

The laptop runner says "this session is asking" inside its wholesale emdash
report. A cloud box has no emdash and makes no such report: it drives the agent
over ACP, where an AskUserQuestion arrives as an `elicitation/create` it is
holding open. So it posts the dialog for the ONE session it is about, and clears
it the moment the ask is answered — the same `RunnerBinding.pending_question`
every channel already reads, and the same `pending_answer` drain back.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

MENU = {
    "question": "Pick a colour", "title": "", "body": "", "selected": None,
    "options": [{"number": 1, "label": "Aubergine", "description": ""},
                {"number": 2, "label": "Beige", "description": ""}],
    "source": "acp", "observed_at": 1.0,
}


def _setup(name="cloud-ec2-2"):
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name=name, kind=Runner.CLOUD, host=name, owner=jj,
                                   workspace=ws, status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now())
    session = Session.objects.create(workspace=ws, created_by=jj, origin=Session.ORIGIN_WEB,
                                     title="cloud chat")
    binding = RunnerBinding.objects.create(session=session, runner=runner, host=runner.host,
                                           session_key="0d6f2c1e-1111-2222-3333-444455556666",
                                           thread_key=str(session.id))
    client = Client()
    client.force_login(jj)
    return jj, runner, session, binding, client


def _post(client, runner, session, menu):
    return client.post(f"/api/harness/runners/{runner.id}/session-menu",
                       {"session_id": str(session.id), "menu": menu},
                       content_type="application/json")


def test_a_cloud_ask_lands_where_every_channel_reads_it():
    _jj, runner, session, binding, client = _setup()
    out = _post(client, runner, session, MENU)
    assert out.status_code == 200, out.content
    assert out.json() == {"ok": True}
    binding.refresh_from_db()
    assert binding.pending_question["question"] == "Pick a colour"
    served = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert [o["label"] for o in served["menu"]["options"]] == ["Aubergine", "Beige"]


def test_the_answer_comes_back_through_the_same_drain():
    from apps.canopy_sessions import services as chat_services

    _jj, runner, session, _binding, client = _setup()
    _post(client, runner, session, MENU)
    assert chat_services.answer_menu(session=session, option=1) == "sent"
    answers = client.get(f"/api/harness/runners/{runner.id}/menu-answers").json()["answers"]
    assert [(a["session_id"], a["option"]) for a in answers] == [(str(session.id), 1)]


def test_clearing_it_retracts_the_dialog():
    _jj, runner, session, binding, client = _setup()
    _post(client, runner, session, MENU)
    assert _post(client, runner, session, None).json() == {"ok": True}
    binding.refresh_from_db()
    assert binding.pending_question is None
    assert client.get(f"/api/canopy-sessions/{session.id}").json()["menu"] is None


def test_open_chats_hear_the_edges_and_the_phone_hears_a_new_ask_once():
    _jj, runner, session, _binding, client = _setup()
    with mock.patch("apps.realtime.groups.publish") as publish, \
            mock.patch("apps.push.services.notify_session_question") as push, \
            mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        _post(client, runner, session, MENU)
        _post(client, runner, session, {**MENU, "observed_at": 2.0})  # same ask, seen again
        _post(client, runner, session, None)
    menus = [c.args[1]["menu"] for c in publish.call_args_list
             if c.args[1].get("type") == "session.menu"]
    assert [m and m["question"] for m in menus] == ["Pick a colour", "Pick a colour", None]
    assert push.call_count == 1, "re-stamping the same ask buzzed the phone again"


def _chat_turn(runner, session, jj):
    from apps.harness.models import Turn

    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=f"t-{Turn.objects.count()}",
                               status=Turn.RUNNING, claimed_by=runner,
                               session_key=RunnerBinding.objects.get(session=session).session_key)


def test_the_turn_ending_ends_its_ask():
    """A cloud ask lives exactly as long as the turn holding it open, so the
    finish retires it even if the box never got to."""
    from apps.harness.models import Turn
    from apps.harness.services import finish_turn

    jj, runner, session, binding, client = _setup()
    turn = _chat_turn(runner, session, jj)
    _post(client, runner, session, MENU)
    finish_turn(turn, status=Turn.FAILED, result_note="box went away")
    binding.refresh_from_db()
    assert binding.pending_question is None


def test_a_box_dying_mid_ask_does_not_strand_the_dialog():
    """A dead box's turn ends by lease expiry, not by a finish — and a cloud box
    makes no wholesale report that would ever retract what it left."""
    from datetime import timedelta

    from apps.harness.claim import sweep_expired_leases

    jj, runner, session, binding, client = _setup()
    turn = _chat_turn(runner, session, jj)
    _post(client, runner, session, MENU)
    turn.lease_expires_at = timezone.now() - timedelta(minutes=1)
    turn.save(update_fields=["lease_expires_at"])
    assert sweep_expired_leases() == 1
    binding.refresh_from_db()
    assert binding.pending_question is None


def test_a_turn_ending_leaves_a_laptop_screen_dialog_alone():
    """A laptop's menu is what its terminal shows, which outlives a turn (an agent
    turn finishes the moment its prompt lands) — only the report clears that."""
    from apps.harness.models import Turn
    from apps.harness.services import finish_turn

    jj, runner, session, binding, _client = _setup()
    binding.pending_question = {**MENU, "source": "hook"}
    binding.save(update_fields=["pending_question"])
    finish_turn(_chat_turn(runner, session, jj), status=Turn.DONE)
    binding.refresh_from_db()
    assert binding.pending_question["source"] == "hook"


def test_a_runner_cannot_put_a_menu_on_a_session_it_does_not_hold():
    jj, _runner, session, binding, client = _setup()
    other = Runner.objects.create(name="other-box", kind=Runner.CLOUD, host="other", owner=jj,
                                  workspace=binding.runner.workspace, status=Runner.ONLINE,
                                  last_heartbeat_at=timezone.now())
    assert _post(client, other, session, MENU).json() == {"ok": False}
    binding.refresh_from_db()
    assert binding.pending_question is None
