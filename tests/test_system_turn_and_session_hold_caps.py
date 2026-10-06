"""Two rules the design states that a SYSTEM turn and a chat turn used to escape.

1. **A workspace editor's work is always manual** (docs/architecture/access.md).
   A schedule's occurrence is a SYSTEM turn, and `access.decide` caps nothing for
   SYSTEM — so an editor's schedule ran `auto` where their own dispatch could
   not. The occurrence is now bounded by its accountable person (the creator).
2. **A box holds an agent only if its owner is one of the agent's admins**
   (`runner_may_hold_agent`). The claim checked it for agent turns only; a turn
   in a chat WITH that agent runs as the agent just the same.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session
from apps.harness import services
from apps.harness import turn_mode as modes
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db



def _slot():
    # Now, not a fixed date: a slot already past its start window is skipped
    # at claim (skip_late_scheduled_turns) before the mode is ever decided.
    return timezone.now().replace(microsecond=0)


@pytest.fixture
def world():
    users = get_user_model().objects
    jj = users.create_user(username="jj", email="jj@dimagi.com")
    ed = users.create_user(username="ed", email="ed@dimagi.com")
    adm = users.create_user(username="adm", email="adm@dimagi.com")
    ada_login = users.create_user(username="ada", email="ada@dimagi-ai.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    for user, role in ((jj, WorkspaceMembership.OWNER), (ed, WorkspaceMembership.EDITOR),
                       (adm, WorkspaceMembership.EDITOR), (ada_login, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=jj,
                               user=ada_login, turn_mode=modes.AUTO)
    AgentAdmin.objects.create(agent=ada, user=adm, granted_by=jj)
    now = timezone.now()
    jj_box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=now,
                                   capabilities={"sessions": True})
    ed_box = Runner.objects.create(name="ed-mbp", kind=Runner.EMDASH, owner=ed, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=now,
                                   capabilities={"sessions": True})
    return {"jj": jj, "ed": ed, "adm": adm, "ada_login": ada_login, "ada": ada,
            "jj_box": jj_box, "ed_box": ed_box}


def _schedule(agent, created_by, name="Fleet review cycle", **extra):
    return AgentSchedule.objects.create(
        agent=agent, name=name, prompt="/ada:conduct",
        cron="0 9 * * 5", timezone="UTC", created_by=created_by, **extra,
    )


# -- 1. a schedule is bounded by the person accountable for it -----------------

def _claim_mode(world, turn):
    RunnerAssignment.objects.get_or_create(agent=world["ada"], runner=world["jj_box"], rank=0)
    claimed = services.claim_next_turn(world["jj_box"])
    assert claimed is not None and claimed.pk == turn.pk
    return claimed


def test_an_editors_schedule_runs_manual_and_says_why(world):
    turn, _ = services.fire_schedule(_schedule(world["ada"], world["ed"]), _slot())
    claimed = _claim_mode(world, turn)
    assert claimed.turn_mode == modes.MANUAL
    assert "ed@dimagi.com" in claimed.turn_mode_basis
    assert "not an admin of ada" in claimed.turn_mode_basis


def test_an_editors_run_now_and_one_off_are_capped_too(world):
    sched = _schedule(world["ada"], world["ed"])
    now_turn = services.run_schedule_now(sched, clicked_by=world["jj"])
    assert modes.for_turn(now_turn, fresh=True).mode == modes.MANUAL
    slot = _slot()
    one_off = _schedule(world["ada"], world["ed"], name="once", run_once_at=slot)
    turn, _ = services.fire_schedule(one_off, slot)
    assert modes.for_turn(turn, fresh=True).mode == modes.MANUAL


@pytest.mark.parametrize("who", ["jj", "adm", "ada_login", None])
def test_an_admins_the_agents_own_or_an_unattributed_schedule_keeps_auto(world, who):
    """Owner, explicit admin, the agent's own login (Ada's live schedule), or no
    recorded creator (Ada's "Fleet review cycle", created_by=null) — unchanged."""
    creator = world[who] if who else None
    turn, _ = services.fire_schedule(_schedule(world["ada"], creator), _slot())
    claimed = _claim_mode(world, turn)
    assert claimed.turn_mode == modes.AUTO
    assert claimed.turn_mode_basis == "agent"


def test_promoting_the_creator_lifts_the_cap_at_the_next_claim(world):
    turn, _ = services.fire_schedule(_schedule(world["ada"], world["ed"]), _slot())
    assert modes.for_turn(turn, fresh=True).mode == modes.MANUAL
    AgentAdmin.objects.create(agent=world["ada"], user=world["ed"], granted_by=world["jj"])
    assert modes.for_turn(turn, fresh=True).mode == modes.AUTO


# -- 2. a chat with an agent is held to the agent's holding rule --------------

def _chat_turn(world, key="c1"):
    session = Session.objects.create(agent=world["ada"], workspace=world["ada"].workspace,
                                     title="chat", created_by=world["jj"])
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=key, routing=Turn.ANY)


def test_a_box_that_cannot_hold_the_agent_cannot_take_its_chat(world):
    RunnerAssignment.objects.create(agent=world["ada"], runner=world["ed_box"], rank=0)
    turn = _chat_turn(world)
    assert services.claim_next_turn(world["ed_box"]) is None
    # Not even pinned: the hold gate sits above the pin, as for an agent turn.
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=world["ed_box"])
    assert services.claim_next_turn(world["ed_box"]) is None


def test_a_box_whose_owner_holds_the_agent_still_takes_its_chat(world):
    RunnerAssignment.objects.create(agent=world["ada"], runner=world["jj_box"], rank=0)
    turn = _chat_turn(world)
    claimed = services.claim_next_turn(world["jj_box"])
    assert claimed is not None and claimed.pk == turn.pk


def test_the_stuck_warning_names_the_hold_and_agrees_with_claiming(world):
    RunnerAssignment.objects.create(agent=world["ada"], runner=world["ed_box"], rank=0)
    turn = _chat_turn(world)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - services.UNCLAIMABLE_GRACE - dt.timedelta(seconds=30))
    assert services.claim_next_turn(world["ed_box"]) is None
    [stuck] = services.unclaimable_queued_turns(world["jj"])
    assert stuck["kind"] == "config"
    assert "admin" in stuck["reason"] and "'ada'" in stuck["reason"]
    assert services.turn_reach(turn).kind == "config"
