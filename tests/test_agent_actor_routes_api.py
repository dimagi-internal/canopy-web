"""GET|PUT|DELETE /api/agents/{slug}/actor-routes[/{actor}] — per-person routing.

The case this exists for: a teammate runs an agent on their own laptop (paired
under the AGENT's identity, administered by them through a RunnerAdmin grant) and
wants to say "route Alice's work to my box" in one call, without being able to
clobber — or even having to re-send — everyone else's rules.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment, Turn, TurnEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    sarvesh = User.objects.create_user(username="st", email="stewari@dimagi.com")
    ace_user = User.objects.create_user(username="ace", email="ace@dimagi-ai.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=sarvesh, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=ace_user, role=WorkspaceMembership.EDITOR)
    # `user` links the agent's own login: a box paired under the agent's
    # identity is the agent's box (`runner_may_hold_agent`).
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, user=ace_user)
    now = timezone.now()
    jj_laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=now, capabilities={},
    )
    # Paired under the agent's identity — the operator is NOT the owner.
    st_laptop = Runner.objects.create(
        name="st-mbp", kind=Runner.EMDASH, owner=ace_user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=now, capabilities={},
    )
    RunnerAdmin.objects.create(runner=st_laptop, user=sarvesh, granted_by=ace_user)
    RunnerAssignment.objects.create(agent=ace, runner=jj_laptop, rank=0)
    return {"client": client, "jj": jj, "sarvesh": sarvesh, "ace": ace,
            "jj_laptop": jj_laptop, "st_laptop": st_laptop}


def _route(client, actor, *runners, **body):
    return client.put(
        f"/api/agents/ace/actor-routes/{actor}",
        data={"runners": [{"runner_id": str(r.id)} for r in runners], **body},
        content_type="application/json",
    )


def test_one_call_routes_a_person_on_every_actor_source(fleet):
    fleet["client"].force_login(fleet["sarvesh"])
    res = _route(fleet["client"], "Alice <Alice@Dimagi.com>", fleet["st_laptop"])
    assert res.status_code == 200, res.content
    [route] = res.json()
    assert route["actor"] == "alice@dimagi.com"
    assert route["strict"] is True
    assert set(route["sources"]) == {"ace_web", "email", "canopy_web_chat", "slack", "api"}
    assert [r["runner_name"] for r in route["runners"]] == ["st-mbp"]


def test_routing_one_person_leaves_everyone_elses_rules_alone(fleet):
    """The wholesale PUT's failure mode, and the reason this route exists."""
    RunnerAssignment.objects.create(
        agent=fleet["ace"], runner=fleet["jj_laptop"], rank=0,
        source="email", actor="bob@dimagi.com", strict=True,
    )
    RunnerAssignment.objects.create(
        agent=fleet["ace"], runner=fleet["jj_laptop"], rank=0, source="slack", strict=False,
    )
    fleet["client"].force_login(fleet["sarvesh"])
    assert _route(fleet["client"], "alice@dimagi.com", fleet["st_laptop"]).status_code == 200
    assert RunnerAssignment.objects.filter(actor="bob@dimagi.com").count() == 1
    assert RunnerAssignment.objects.filter(source="slack", actor="").count() == 1
    assert RunnerAssignment.objects.filter(source="").count() == 1


def test_a_second_put_replaces_that_persons_route(fleet):
    RunnerAdmin.objects.create(runner=fleet["jj_laptop"], user=fleet["sarvesh"])
    fleet["client"].force_login(fleet["sarvesh"])
    assert _route(fleet["client"], "alice@dimagi.com", fleet["jj_laptop"]).status_code == 200
    res = _route(fleet["client"], "alice@dimagi.com", fleet["st_laptop"],
                 sources=["slack"], strict=False)
    [route] = res.json()
    assert route["sources"] == ["slack"]
    assert route["strict"] is False
    assert RunnerAssignment.objects.filter(actor="alice@dimagi.com").count() == 1


def test_cannot_route_work_onto_a_box_you_do_not_administer(fleet):
    """Sarvesh may point people at his box — not at Jonathan's."""
    fleet["client"].force_login(fleet["sarvesh"])
    res = _route(fleet["client"], "alice@dimagi.com", fleet["jj_laptop"])
    assert res.status_code == 403
    assert "jj-mbp" in res.json()["detail"]
    assert not RunnerAssignment.objects.filter(actor="alice@dimagi.com").exists()


def test_a_scheduler_route_is_refused_because_no_person_is_behind_it(fleet):
    fleet["client"].force_login(fleet["sarvesh"])
    res = _route(fleet["client"], "alice@dimagi.com", fleet["st_laptop"],
                 sources=["canopy_scheduler"])
    assert res.status_code == 422


def test_a_non_address_is_refused(fleet):
    fleet["client"].force_login(fleet["sarvesh"])
    assert _route(fleet["client"], "alice", fleet["st_laptop"]).status_code == 422


def test_a_viewer_cannot_route(fleet):
    WorkspaceMembership.objects.filter(user=fleet["sarvesh"]).update(role=WorkspaceMembership.VIEWER)
    fleet["client"].force_login(fleet["sarvesh"])
    assert _route(fleet["client"], "alice@dimagi.com", fleet["st_laptop"]).status_code == 403


def test_delete_removes_only_that_person(fleet):
    fleet["client"].force_login(fleet["sarvesh"])
    _route(fleet["client"], "alice@dimagi.com", fleet["st_laptop"])
    _route(fleet["client"], "carol@dimagi.com", fleet["st_laptop"])
    res = fleet["client"].delete("/api/agents/ace/actor-routes/alice@dimagi.com")
    assert res.status_code == 204
    got = fleet["client"].get("/api/agents/ace/actor-routes").json()
    assert [r["actor"] for r in got] == ["carol@dimagi.com"]


def test_a_routed_persons_slack_turn_claims_on_their_box_and_others_stay_on_the_default(fleet):
    """End to end: the route decides who CLAIMS, which is the only thing that matters."""
    fleet["client"].force_login(fleet["sarvesh"])
    _route(fleet["client"], "stewari@dimagi.com", fleet["st_laptop"])

    Turn.objects.create(agent=fleet["ace"], origin=Turn.ORIGIN_SLACK, idempotency_key="s1",
                        routing=Turn.ANY, enqueued_by=fleet["sarvesh"])
    Turn.objects.create(agent=fleet["ace"], origin=Turn.ORIGIN_SLACK, idempotency_key="j1",
                        routing=Turn.ANY, enqueued_by=fleet["jj"])

    jj_claim = services.claim_next_turn(fleet["jj_laptop"])
    assert jj_claim is not None and jj_claim.idempotency_key == "j1"
    assert services.claim_next_turn(fleet["jj_laptop"]) is None  # Sarvesh's is not his to take
    Turn.objects.filter(pk=jj_claim.pk).update(status=Turn.DONE)
    st_claim = services.claim_next_turn(fleet["st_laptop"])
    assert st_claim is not None and st_claim.idempotency_key == "s1"

    # The claim event says WHY, so a routing audit needn't re-derive the ladder.
    def basis(turn):
        ev = TurnEvent.objects.filter(turn=turn, kind="status").order_by("seq").last()
        return ev.payload["actor"], ev.payload["rule"]

    assert basis(st_claim) == ("stewari@dimagi.com", "actor")
    assert basis(jj_claim) == ("jj@dimagi.com", "default")


def test_a_contact_who_is_not_a_member_can_be_routed(fleet):
    """A Slack sender outside the workspace (and every ace-web widget user) arrives
    as a CONTACT with no `enqueued_by`. Their route must still match — "route
    Jeremy to my laptop" is the case this feature was asked for."""
    from apps.contacts.models import Contact

    jeremy = Contact.objects.create(workspace=fleet["ace"].workspace, email="jwacksman@dimagi.com")
    fleet["client"].force_login(fleet["sarvesh"])
    _route(fleet["client"], "jwacksman@dimagi.com", fleet["st_laptop"])
    Turn.objects.create(agent=fleet["ace"], origin=Turn.ORIGIN_SLACK, idempotency_key="c1",
                        routing=Turn.ANY, initiator_contact=jeremy)

    assert services.claim_next_turn(fleet["jj_laptop"]) is None
    claimed = services.claim_next_turn(fleet["st_laptop"])
    assert claimed is not None and claimed.idempotency_key == "c1"
