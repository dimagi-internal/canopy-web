"""A runner whose Claude login is gone claims nothing, and a turn waiting on it says so.

2026-10-22: acedimagi-mbp-cdp was online with no Claude sign-in. The chat said
"Picking this up on acedimagi-mbp-cdp…", the box took each message, and each
"reply" was the CLI's `Not logged in · Please run /login`. The box now reports
`canopy_transcript.SIGNED_OUT_NOTE` as its readiness note; this pins what the
server does with it.
"""
from __future__ import annotations

import pytest
from canopy_transcript import SIGNED_OUT_NOTE
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import claim
from apps.harness import turn_status as ts
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    mac = Runner.objects.create(
        name="acedimagi-mbp-cdp", kind=Runner.EMDASH, owner=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={"sessions": True},
    )
    RunnerAssignment.objects.create(agent=ace, runner=mac, rank=0)
    session = Session.objects.create(agent=ace, workspace=ws, title="chat", created_by=jj)
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key="t1", routing=Turn.ANY, enqueued_by=jj)
    return {"mac": mac, "turn": turn}


def _sign_out(runner):
    Runner.objects.filter(pk=runner.pk).update(
        ready=False, ready_note=SIGNED_OUT_NOTE + " — sign in with `claude /login` there")
    runner.refresh_from_db()


def test_signed_out_is_only_the_signed_out_note(fleet):
    mac = fleet["mac"]
    assert not mac.signed_out
    # Another not-ready reason still runs turns (emdash fallback) — not this.
    Runner.objects.filter(pk=mac.pk).update(ready=False, ready_note="emdash CDP unreachable")
    mac.refresh_from_db()
    assert not mac.signed_out
    _sign_out(mac)
    assert mac.signed_out
    assert "/login" in mac.remedy


def test_a_signed_out_runner_claims_nothing(fleet):
    mac = fleet["mac"]
    _sign_out(mac)
    assert claim.claim_next_turn(mac) is None
    Runner.objects.filter(pk=mac.pk).update(ready=True, ready_note="")
    mac.refresh_from_db()
    claimed = claim.claim_next_turn(mac)
    assert claimed is not None and claimed.pk == fleet["turn"].pk


def test_a_signed_out_pin_does_not_punch_through(fleet):
    mac, turn = fleet["mac"], fleet["turn"]
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=mac)
    _sign_out(mac)
    assert claim.claim_next_turn(mac) is None


def test_the_waiting_turn_says_signed_out_not_picking_up(fleet):
    mac, turn = fleet["mac"], fleet["turn"]
    assert ts.resolve(turn).state == ts.PICKING_UP
    _sign_out(mac)
    assert claim.turn_reach(turn).kind == claim.SIGNED_OUT
    st = ts.resolve(turn)
    assert st.state == ts.SIGNED_OUT
    assert st.runners == ("acedimagi-mbp-cdp",)
    assert st.stuck


def test_the_slack_line_names_the_sign_in(fleet):
    from apps.slack import status

    _sign_out(fleet["mac"])
    turn = Turn.objects.select_related("chat_session", "chat_session__agent").get(pk=fleet["turn"].pk)
    text, _blocks = status.render(turn, reach=claim.turn_reach(turn))
    assert "not signed in" in text and "acedimagi-mbp-cdp" in text
