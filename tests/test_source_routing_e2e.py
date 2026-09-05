"""The three cases this feature was built for, end to end through the API.

Deliberately driven through HTTP rather than the service layer: the operator
configures this in the Runners tab, and the thing that must work is the whole
path from that PUT to which runner claims.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={"sessions": True},
    )
    for agent in (ace, echo):
        RunnerAssignment.objects.create(agent=agent, runner=laptop, rank=0)
        RunnerAssignment.objects.create(agent=agent, runner=cloud, rank=1)
    client.force_login(jj)
    return {"client": client, "ace": ace, "echo": echo, "laptop": laptop, "cloud": cloud}


def _rule(client, slug, source, runner, strict, actor=""):
    """A rule now names an ORDERED LIST of runners (spec 2026-09-05); this helper
    keeps posting one, so every assertion below is unchanged — which is the point:
    a one-runner, no-actor rule must behave exactly as it did before actors."""
    res = client.put(
        f"/api/agents/{slug}/runner-rules",
        data={"rules": [{
            "source": source,
            "actor": actor,
            "runners": [{"runner_id": str(runner.id), "enabled": True}],
            "strict": strict,
        }]},
        content_type="application/json",
    )
    assert res.status_code == 200, res.content


def _queue(agent, origin, key, **kw):
    return Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=key, routing=Turn.ANY, **kw
    )


def test_ace_web_work_runs_on_the_cloud_runner(fleet):
    """Case 1. ace-web delegates execution to canopy-web; that work is why the
    cloud runner exists, so it must not land on the laptop that outranks it."""
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    res = fleet["client"].post(
        "/api/harness/turns/",
        data={"agent_slug": "ace", "origin": "ace_web", "idempotency_key": "e2e-ace",
              "prompt": "/ace:turn", "routing": "any"},
        content_type="application/json",
    )
    assert res.status_code == 201, res.content

    assert services.claim_next_turn(fleet["laptop"]) is None
    claimed = services.claim_next_turn(fleet["cloud"])
    assert claimed is not None and claimed.agent_id == fleet["ace"].id


def test_email_work_stays_on_the_laptop(fleet):
    """Case 2. The inbox watcher enqueues these UNPINNED from whichever box
    polled, so without a rule the cloud box could answer mail the laptop found."""
    _rule(fleet["client"], "echo", "email", fleet["laptop"], True)
    _queue(
        fleet["echo"], Turn.ORIGIN_EMAIL, "email-echo-t1-1",
        origin_ref={"thread_id": "t1", "from": "someone@example.com"},
        prompt="/echo:turn --thread t1",
    )

    assert services.claim_next_turn(fleet["cloud"]) is None
    assert services.claim_next_turn(fleet["laptop"]) is not None


def test_scheduled_work_prefers_the_cloud_but_still_degrades(fleet):
    """Case 3. Non-strict: the lid can be shut at 6am, but a dead cloud box must
    not park the schedule forever."""
    _rule(fleet["client"], "echo", "canopy_scheduler", fleet["cloud"], False)
    _queue(
        fleet["echo"], Turn.ORIGIN_CANOPY_SCHEDULER, "sched:1:2026-07-27T06:00",
        prompt="/echo:turn",
    )

    assert services.claim_next_turn(fleet["laptop"]) is None   # cloud is up, it goes first
    assert services.claim_next_turn(fleet["cloud"]) is not None


def test_a_fall_through_rule_degrades_to_the_laptop_when_the_cloud_is_down(fleet):
    """The other half of case 3 — the reason it is not strict."""
    _rule(fleet["client"], "echo", "canopy_scheduler", fleet["cloud"], False)
    Runner.objects.filter(pk=fleet["cloud"].pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )
    _queue(fleet["echo"], Turn.ORIGIN_CANOPY_SCHEDULER, "sched:1:0700", prompt="/echo:turn")

    assert services.claim_next_turn(fleet["laptop"]) is not None


def test_the_three_rules_coexist_on_one_fleet(fleet):
    fleet["client"].put(
        "/api/agents/echo/runner-rules",
        data={"rules": [
            {"source": "email", "runner_id": str(fleet["laptop"].id), "strict": True},
            {"source": "canopy_scheduler", "runner_id": str(fleet["cloud"].id), "strict": False},
        ]},
        content_type="application/json",
    )
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    _queue(fleet["echo"], Turn.ORIGIN_EMAIL, "m1")
    _queue(fleet["ace"], Turn.ORIGIN_ACE_WEB, "a1")

    first = services.claim_next_turn(fleet["laptop"])
    second = services.claim_next_turn(fleet["cloud"])

    assert first is not None and first.origin == Turn.ORIGIN_EMAIL
    assert second is not None and second.origin == Turn.ORIGIN_ACE_WEB


# --- actor rules: route by WHO, not just what kind (spec 2026-09-05) ----------

def _rules(client, slug, rules):
    """Several rules at once — the wholesale-replace body the Runners tab sends."""
    res = client.put(
        f"/api/agents/{slug}/runner-rules",
        data={"rules": rules}, content_type="application/json",
    )
    assert res.status_code == 200, res.content


def _runners_of(rule):
    return [{"runner_id": str(r.id), "enabled": True} for r in rule]


def test_one_persons_mail_goes_to_cloud_while_everyone_elses_stays_local(fleet):
    """THE case multiplayer was blocked on.

    ACE keeps the operator's boxes as its default order and allowlists named
    colleagues onto the cloud runner — so other people can use ace-web without
    their work landing on the machine the operator is debugging on.
    """
    _rules(fleet["client"], "ace", [{
        "source": "email", "actor": "stewari@dimagi.com",
        "runners": _runners_of([fleet["cloud"]]), "strict": True,
    }])

    theirs = _queue(fleet["ace"], Turn.ORIGIN_EMAIL, "k-sarvesh",
                    origin_ref={"from": "Sarvesh Tewari <STewari@Dimagi.com>"})
    mine = _queue(fleet["ace"], Turn.ORIGIN_EMAIL, "k-jj",
                  origin_ref={"from": "Jonathan Jackson <jjackson@dimagi.com>"})

    # The laptop outranks cloud in the default order and Sarvesh's turn is the
    # OLDER of the two — yet the laptop skips straight past it to the operator's
    # own mail, because the strict rule removes the laptop from Sarvesh's list.
    assert services.claim_next_turn(fleet["laptop"]).id == mine.id

    # `one_executing_turn_per_agent` now holds ace's slot, so free it before
    # asking who takes Sarvesh's — otherwise this asserts the index, not routing.
    services.finish_turn(mine, status=Turn.DONE)

    assert services.claim_next_turn(fleet["cloud"]).id == theirs.id


def test_a_strict_operator_rule_survives_one_of_the_two_accounts_being_down(fleet):
    """OPERATOR_BOXES. jj-mbp and acedimagi-mbp are two macOS accounts on ONE
    machine, alternated as each runs out of tokens — so a rule naming only the
    logged-out one would park roughly half the time. Naming both is the fix, and
    this is the assertion that proves a one-runner rule could not have done it."""
    acedimagi = Runner.objects.create(
        name="acedimagi-mbp", kind=Runner.EMDASH, paired_by=fleet["laptop"].paired_by,
        status=Runner.DISCONNECTED, last_heartbeat_at=timezone.now() - dt.timedelta(hours=2),
        capabilities={},
    )
    # echo is cloud-DEFAULT: without the rule, the operator's own work goes to cloud.
    RunnerAssignment.objects.filter(agent=fleet["echo"], runner=fleet["cloud"]).update(rank=0)
    RunnerAssignment.objects.filter(agent=fleet["echo"], runner=fleet["laptop"]).update(rank=1)
    _rules(fleet["client"], "echo", [{
        "source": "email", "actor": "jjackson@dimagi.com",
        # acedimagi first — it is normally the live account — then jj-mbp.
        "runners": _runners_of([acedimagi, fleet["laptop"]]), "strict": True,
    }])

    _queue(fleet["echo"], Turn.ORIGIN_EMAIL, "k-mine",
           origin_ref={"from": "Jonathan Jackson <jjackson@dimagi.com>"})

    # The preferred account is offline, so the rule degrades WITHIN itself …
    assert services.claim_next_turn(fleet["laptop"]) is not None
    # … and never to cloud, which outranks both in the default order.
    assert services.claim_next_turn(fleet["cloud"]) is None


def test_a_strict_rule_parks_rather_than_leaking_to_cloud_past_the_grace(fleet):
    """The wedged-runner grace opens a turn to lower ranks after 60s. A strict
    rule's excluded runners are absent from the list entirely, so there is nobody
    for the grace to promote — including past the grace."""
    RunnerAssignment.objects.filter(agent=fleet["echo"], runner=fleet["cloud"]).update(rank=0)
    _rules(fleet["client"], "echo", [{
        "source": "email", "actor": "jjackson@dimagi.com",
        "runners": _runners_of([fleet["laptop"]]), "strict": True,
    }])
    old = _queue(fleet["echo"], Turn.ORIGIN_EMAIL, "k-old",
                 origin_ref={"from": "jjackson@dimagi.com"})
    Turn.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - dt.timedelta(seconds=services.CASCADE_GRACE_SECONDS + 30)
    )

    assert services.claim_next_turn(fleet["cloud"]) is None
    assert services.claim_next_turn(fleet["laptop"]).id == old.id


def test_a_scheduled_turn_matches_no_actor_rule(fleet):
    """A schedule has no human actor, so it must fall through to the source rule
    rather than match somebody's rule by accident."""
    _rules(fleet["client"], "echo", [
        {"source": "canopy_scheduler", "actor": "jjackson@dimagi.com",
         "runners": _runners_of([fleet["laptop"]]), "strict": True},
        {"source": "canopy_scheduler", "actor": "",
         "runners": _runners_of([fleet["cloud"]]), "strict": True},
    ])
    fired = _queue(fleet["echo"], Turn.ORIGIN_CANOPY_SCHEDULER, "k-sched",
                   origin_ref={"schedule_id": 1, "slot": "2026-09-05T00:00:00Z"})

    assert services.claim_next_turn(fleet["laptop"]) is None
    assert services.claim_next_turn(fleet["cloud"]).id == fired.id


def test_claim_and_unclaimable_agree_per_actor(fleet):
    """The parity discipline, extended to the actor key. A strict actor rule
    pointing at a box that is merely OFFLINE must read as recoverable — if these
    two disagree, the UI tells the operator a live queue will never run."""
    fleet["laptop"].status = Runner.DISCONNECTED
    fleet["laptop"].last_heartbeat_at = timezone.now() - dt.timedelta(hours=2)
    fleet["laptop"].save()
    _rules(fleet["client"], "ace", [{
        "source": "email", "actor": "jjackson@dimagi.com",
        "runners": _runners_of([fleet["laptop"]]), "strict": True,
    }])
    parked = _queue(fleet["ace"], Turn.ORIGIN_EMAIL, "k-parked",
                    origin_ref={"from": "jjackson@dimagi.com"})
    # Age it past UNCLAIMABLE_GRACE — the warning deliberately ignores turns that
    # have only just been queued, so a fresh one reports nothing at all.
    Turn.objects.filter(pk=parked.pk).update(
        created_at=timezone.now() - services.UNCLAIMABLE_GRACE - dt.timedelta(minutes=1)
    )

    # Nobody can claim it right now …
    assert services.claim_next_turn(fleet["cloud"]) is None
    # … and the warning says OFFLINE (wait for the box), never CONFIG (never runs).
    # `kind` is the classification the UI branches on; `reason` is its prose.
    reported = {
        r["turn_id"]: r["kind"]
        for r in services.unclaimable_queued_turns(fleet["laptop"].paired_by)
    }
    assert reported.get(str(parked.id)) == "offline"
