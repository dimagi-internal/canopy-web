"""GET|PUT /api/agents/{slug}/runner-rules.

The wipe tests are the important ones: both writes live in the same table, and a
default-list save that silently deleted every rule is exactly the bug this
endpoint split exists to prevent.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    client.force_login(jj)
    return {"client": client, "agent": agent, "laptop": laptop, "cloud": cloud}


def _put_rules(client, rules):
    return client.put(
        "/api/agents/echo/runner-rules",
        data={"rules": rules}, content_type="application/json",
    )


def _put_default(client, runner):
    return client.put(
        "/api/agents/echo/runners",
        data={"runners": [{"runner_id": str(runner.id), "enabled": True}]},
        content_type="application/json",
    )


def test_put_then_get_round_trips_a_rule(setup):
    res = _put_rules(setup["client"], [
        {"source": "ace_web", "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}], "strict": True},
    ])
    assert res.status_code == 200, res.content

    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert len(got) == 1
    assert got[0]["source"] == "ace_web"
    assert got[0]["runner_name"] == "cloud-1"
    assert got[0]["strict"] is True
    assert got[0]["online"] is True


def test_saving_the_default_list_does_not_wipe_the_rules(setup):
    """RunnerAssignment holds both; PUT /runners must scope its delete to source=''."""
    _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}], "strict": True},
    ])

    assert _put_default(setup["client"], setup["cloud"]).status_code == 200

    assert RunnerAssignment.objects.filter(agent=setup["agent"], source="email").exists()


def test_saving_the_rules_does_not_wipe_the_default_list(setup):
    _put_default(setup["client"], setup["cloud"])

    _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}], "strict": True},
    ])

    assert RunnerAssignment.objects.filter(agent=setup["agent"], source="").count() == 1


def test_the_default_list_read_excludes_rule_rows(setup):
    """Otherwise a rule would show up as a phantom chip in the Default order row."""
    _put_default(setup["client"], setup["cloud"])
    _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}]},
    ])

    got = setup["client"].get("/api/agents/echo/runners").json()

    assert [r["runner_name"] for r in got] == ["cloud-1"]


def test_put_replaces_wholesale(setup):
    _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}], "strict": True},
    ])
    _put_rules(setup["client"], [
        {"source": "ace_web", "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}], "strict": False},
    ])

    rows = RunnerAssignment.objects.filter(agent=setup["agent"]).exclude(source="")
    assert [r.source for r in rows] == ["ace_web"]


def test_a_duplicate_source_and_actor_is_rejected(setup):
    """Two rules on the same (source, actor). Two rules on the same source with
    DIFFERENT actors is now legal — that is the feature."""
    res = _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}]},
        {"source": "email", "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
    ])
    assert res.status_code == 422


def test_a_non_routable_source_is_rejected(setup):
    res = _put_rules(setup["client"], [
        {"source": "not_a_source", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}]},
    ])
    assert res.status_code == 422


def test_a_runner_the_caller_cannot_see_is_rejected(setup):
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )

    res = _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(theirs.id), "enabled": True}]},
    ])

    assert res.status_code == 422
    # …and the rejected batch left nothing behind: the delete is inside the same
    # transaction as the insert, so a bad row can't wipe the existing rules.
    assert not RunnerAssignment.objects.filter(agent=setup["agent"]).exclude(source="").exists()


def test_queued_count_reports_the_parked_work(setup):
    """The UI's 'N turns are parked' warning reads this."""
    _put_rules(setup["client"], [
        {"source": "email", "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}], "strict": True},
    ])
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="q1")
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="q2")
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_API, idempotency_key="q3")

    got = setup["client"].get("/api/agents/echo/runner-rules").json()

    assert got[0]["queued_count"] == 2


def test_a_non_member_cannot_read_or_write_the_rules(client):
    """Same 404-not-403 discipline as the rest of the agents surface."""
    owner = get_user_model().objects.create_user(username="own", email="own@dimagi.com")
    ws = Workspace.objects.create(slug="private", display_name="Private", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="Secret", workspace=ws)
    outsider = get_user_model().objects.create_user(username="mal", email="mal@dimagi.com")
    client.force_login(outsider)

    assert client.get("/api/agents/secret/runner-rules").status_code == 404
    assert client.put(
        "/api/agents/secret/runner-rules",
        data={"rules": []}, content_type="application/json",
    ).status_code == 404


# --- actors (spec 2026-09-05) -------------------------------------------------

def test_a_rule_round_trips_its_actor(setup):
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "stewari@dimagi.com",
         "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}], "strict": True},
    ])
    assert res.status_code == 200, res.content

    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert [(r["source"], r["actor"], r["runner_name"]) for r in got] == [
        ("email", "stewari@dimagi.com", "cloud-1"),
    ]


def test_two_actors_may_share_one_source(setup):
    """The whole point: Sarvesh's mail to cloud, mine to my laptop."""
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "stewari@dimagi.com",
         "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
        {"source": "email", "actor": "jj@dimagi.com",
         "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}]},
    ])
    assert res.status_code == 200, res.content
    assert len(setup["client"].get("/api/agents/echo/runner-rules").json()) == 2


def test_a_rule_may_name_several_runners_in_order(setup):
    """OPERATOR_BOXES: two macOS accounts on one machine, whichever is live."""
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "jj@dimagi.com", "strict": True, "runners": [
            {"runner_id": str(setup["laptop"].id), "enabled": True},
            {"runner_id": str(setup["cloud"].id), "enabled": True},
        ]},
    ])
    assert res.status_code == 200, res.content

    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert [(r["rank"], r["runner_name"]) for r in got] == [(0, "jj-mbp"), (1, "cloud-1")]


def test_an_actor_is_normalized_so_a_pasted_header_still_matches(setup):
    """Operators paste what they see in a mail client. A rule written as a full
    From header must match a turn whose sender resolves to the bare address."""
    _put_rules(setup["client"], [
        {"source": "email", "actor": "Sarvesh Tewari <STewari@Dimagi.com>",
         "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
    ])
    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert got[0]["actor"] == "stewari@dimagi.com"


def test_an_unparseable_actor_is_rejected_not_stored_as_a_dead_rule(setup):
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "not an address",
         "runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
    ])
    assert res.status_code == 422


def test_the_same_runner_twice_in_one_rule_is_rejected(setup):
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "jj@dimagi.com", "runners": [
            {"runner_id": str(setup["cloud"].id), "enabled": True},
            {"runner_id": str(setup["cloud"].id), "enabled": True},
        ]},
    ])
    assert res.status_code == 422


def test_a_rule_with_no_runners_is_rejected(setup):
    """A zero-length strict rule would compose to an empty list and park the queue
    with no runner named as the reason. Deleting the rule is how you turn it off."""
    res = _put_rules(setup["client"], [
        {"source": "email", "actor": "jj@dimagi.com", "runners": [], "strict": True},
    ])
    assert res.status_code == 422


def test_queued_count_is_scoped_to_the_rules_actor(setup):
    """Otherwise the parked warning counts other people's work as yours."""
    jj = get_user_model().objects.get(username="jj")
    _put_rules(setup["client"], [
        {"source": "api", "actor": "jj@dimagi.com",
         "runners": [{"runner_id": str(setup["laptop"].id), "enabled": True}], "strict": True},
    ])
    Turn.objects.create(
        agent=setup["agent"], origin=Turn.ORIGIN_API, idempotency_key="mine", enqueued_by=jj,
    )
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_API, idempotency_key="theirs")

    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert got[0]["queued_count"] == 1
