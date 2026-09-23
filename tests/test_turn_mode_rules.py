"""A routing rule may set the MODE its work runs in, not just the box.

`eva / email / beth@… → cloud, auto`: the rule that routes Beth's mail to the
cloud runner also lets Eva act on it without asking first. The mode is decided
at claim from the same ladder that routed the turn — (source, actor) rule, then
(source, anyone) rule, then `Agent.turn_mode` — stamped on the turn, and handed
to the agent in the caller envelope.

The security-relevant test is the unverified one: an `auto` that names a person
applies only to a message that proves it came from them. Routing on a forged
From: picks a box; auto on one would let the forger drive an unreviewed agent.

Spec: docs/superpowers/specs/2026-09-23-per-rule-turn-mode-design.md
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import caller_context, services
from apps.harness import turn_mode as modes
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BETH = "beth@dimagi.com"
# What our receiver writes for a DMARC-aligned message from dimagi.com.
PASS_ALL = ("mx.google.com; dkim=pass header.i=@dimagi.com; spf=pass "
            "smtp.mailfrom=dimagi.com; dmarc=pass header.from=dimagi.com")
VERIFIED = [{"name": "Authentication-Results", "value": PASS_ALL}]


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, turn_mode=Agent.MANUAL)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    # Session-capable: an email with a thread id becomes a SESSION turn (the
    # thread is the conversation), and only such a runner claims one.
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={"sessions": True},
    )
    RunnerAssignment.objects.create(agent=eva, runner=laptop, rank=0)
    client.force_login(jj)
    return {"client": client, "agent": eva, "laptop": laptop, "cloud": cloud}


def _rule(agent, runner, *, source=Turn.ORIGIN_EMAIL, actor="", mode="", strict=False):
    RunnerAssignment.objects.create(
        agent=agent, runner=runner, rank=0, source=source, actor=actor,
        strict=strict, turn_mode=mode,
    )


def _mail(agent, sender=BETH, key="m-1", headers=None):
    ref = {"from": sender, "subject": "hi", "thread_id": key}
    if headers is not None:
        ref["headers"] = headers
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key, origin_ref=ref,
    )
    return turn


# --- the ladder ---------------------------------------------------------------

def test_with_no_rule_the_turn_takes_the_agents_mode(fleet):
    a = fleet["agent"]
    Agent.objects.filter(pk=a.pk).update(turn_mode=Agent.AUTO)
    got = modes.for_turn(_mail(Agent.objects.get(pk=a.pk)))
    assert (got.mode, got.basis) == ("auto", "agent")


def test_a_verified_message_from_the_named_sender_runs_auto(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, actor=BETH, mode="auto")
    got = modes.for_turn(_mail(a, headers=VERIFIED))
    assert got.mode == "auto"
    assert got.basis == f"rule email/{BETH}"


def test_an_unverified_message_claiming_to_be_the_named_sender_runs_manual(fleet):
    """The whole reason the mode is not just another routing column."""
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, actor=BETH, mode="auto")
    got = modes.for_turn(_mail(a))  # no Authentication-Results: could be anyone
    assert got.mode == "manual"
    assert "not verified" in got.basis


def test_an_unverified_message_does_not_fall_through_to_an_auto_agent(fleet):
    """Withheld means MANUAL, not "whatever the agent says": the rule was written
    about this person, and a message that may not be them is the one to slow on."""
    a, cloud = fleet["agent"], fleet["cloud"]
    Agent.objects.filter(pk=a.pk).update(turn_mode=Agent.AUTO)
    _rule(a, cloud, actor=BETH, mode="auto")
    assert modes.for_turn(_mail(Agent.objects.get(pk=a.pk))).mode == "manual"


def test_a_manual_rule_is_honoured_even_unverified(fleet):
    """Lowering autonomy is safe whoever asks."""
    a, cloud = fleet["agent"], fleet["cloud"]
    Agent.objects.filter(pk=a.pk).update(turn_mode=Agent.AUTO)
    _rule(a, cloud, actor=BETH, mode="manual")
    got = modes.for_turn(_mail(Agent.objects.get(pk=a.pk)))
    assert (got.mode, got.basis) == ("manual", f"rule email/{BETH}")


def test_an_anyone_rule_needs_no_verification(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, mode="auto")
    got = modes.for_turn(_mail(a, sender="stranger@else.org"))
    assert (got.mode, got.basis) == ("auto", "rule email/anyone")


def test_a_rule_that_says_nothing_about_mode_defers_to_the_next_rung(fleet):
    """Moving Beth's work to another box must not change how it is handled."""
    a, cloud, laptop = fleet["agent"], fleet["cloud"], fleet["laptop"]
    _rule(a, cloud, actor=BETH, mode="")        # routing only
    _rule(a, laptop, mode="auto")               # every email: auto
    got = modes.for_turn(_mail(a, headers=VERIFIED))
    assert (got.mode, got.basis) == ("auto", "rule email/anyone")


def test_a_rule_for_another_source_does_not_apply(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, source=Turn.ORIGIN_SLACK, mode="auto")
    assert modes.for_turn(_mail(a)).basis == "agent"


def test_a_switched_off_rule_takes_its_mode_with_it(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, mode="auto")
    RunnerAssignment.objects.filter(agent=a, source=Turn.ORIGIN_EMAIL).update(enabled=False)
    assert modes.for_turn(_mail(a)).basis == "agent"


def test_a_project_turn_has_no_mode(fleet):
    t = Turn.objects.create(project="canopy-web", origin=Turn.ORIGIN_API, idempotency_key="p")
    assert modes.for_turn(t) is None


# --- stamped at claim, delivered in the envelope ------------------------------

def test_claiming_stamps_the_mode_and_the_envelope_carries_it(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, actor=BETH, mode="auto", strict=True)
    turn = _mail(a, headers=VERIFIED)

    claimed = services.claim_next_turn(cloud)

    assert claimed.pk == turn.pk
    assert (claimed.turn_mode, claimed.turn_mode_basis) == ("auto", f"rule email/{BETH}")
    env = caller_context.build(claimed)
    assert env["turn_mode"] == {"mode": "auto", "basis": f"rule email/{BETH}"}


def test_a_rule_edited_mid_turn_does_not_change_the_running_turns_mode(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, actor=BETH, mode="auto", strict=True)
    _mail(a, headers=VERIFIED)
    claimed = services.claim_next_turn(cloud)

    RunnerAssignment.objects.filter(agent=a, actor=BETH).update(turn_mode="manual")

    assert caller_context.build(claimed)["turn_mode"]["mode"] == "auto"


def test_a_reclaimed_turn_decides_its_mode_again(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, actor=BETH, mode="auto", strict=True)
    turn = _mail(a, headers=VERIFIED)
    services.claim_next_turn(cloud)
    # Lease lost: back to the queue, still carrying the old stamp.
    Turn.objects.filter(pk=turn.pk).update(status=Turn.QUEUED, claimed_by=None)
    RunnerAssignment.objects.filter(agent=a, actor=BETH).update(turn_mode="manual")

    again = services.claim_next_turn(cloud)

    assert again.turn_mode == "manual"


def test_the_claim_response_carries_the_mode(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _rule(a, cloud, mode="auto", strict=True)
    _mail(a)
    res = fleet["client"].post(f"/api/harness/runners/{cloud.id}/claim")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["turn_mode"] == "auto"
    assert body["caller_context"]["turn_mode"]["mode"] == "auto"


# --- the rules API ------------------------------------------------------------

def _put(client, rules):
    return client.put("/api/agents/eva/runner-rules", data={"rules": rules},
                      content_type="application/json")


def test_the_rules_api_round_trips_a_mode(fleet):
    res = _put(fleet["client"], [{
        "source": "email", "actor": "Beth <Beth@Dimagi.com>", "strict": True,
        "turn_mode": "auto",
        "runners": [{"runner_id": str(fleet["cloud"].id), "enabled": True}],
    }])
    assert res.status_code == 200, res.content
    [row] = fleet["client"].get("/api/agents/eva/runner-rules").json()
    assert (row["actor"], row["turn_mode"]) == (BETH, "auto")


def test_the_rules_api_defaults_to_no_mode(fleet):
    _put(fleet["client"], [{
        "source": "email", "runners": [{"runner_id": str(fleet["cloud"].id), "enabled": True}],
    }])
    [row] = fleet["client"].get("/api/agents/eva/runner-rules").json()
    assert row["turn_mode"] == ""


def test_the_rules_api_rejects_an_unknown_mode(fleet):
    res = _put(fleet["client"], [{
        "source": "email", "turn_mode": "yolo",
        "runners": [{"runner_id": str(fleet["cloud"].id), "enabled": True}],
    }])
    assert res.status_code == 422

