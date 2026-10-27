"""A person can dedicate a runner to a list of agents (`Runner.only_agents`).

Listed, the box runs only those agents' turns and chats — no other agent's, no
repo turn, no agentless chat — however they reach it, a pin included. It never
adds work: an agent still reaches the box only through its runner list or the
order it follows. The person-set counterpart of a box declaring it runs one agent
(`test_runners_on_one_machine.py`), asked through the same `routing.serves_refusal`, so the
claim, the turn status, the unclaimable report and the vault agree.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import claim, routing
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    box = Runner.objects.create(name="cloud-zdr-1", kind=Runner.CLOUD, host="cloud-zdr-1", owner=jj,
                                workspace=dimagi, status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now(),
                                capabilities={"sessions": True, "projects": ["canopy-web"]})
    labsdata = Agent.objects.create(slug="labsdata", name="labsdata", workspace=dimagi, owner=jj)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=dimagi, owner=jj)
    RunnerAssignment.objects.create(agent=labsdata, runner=box, rank=0)
    RunnerAssignment.objects.create(agent=ace, runner=box, rank=0)
    return {"jj": jj, "dimagi": dimagi, "box": box, "labsdata": labsdata, "ace": ace}


def _dedicate(runner, *agents):
    runner.only_agents.set(agents)
    return Runner.objects.get(pk=runner.pk)  # a fresh row: no cached list


def _turn(key, *, age=3600, **kw):
    t = Turn.objects.create(origin=Turn.ORIGIN_API, idempotency_key=key, routing=Turn.ANY,
                            prompt="hi", **kw)
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age))
    return t


def _chat(fleet, agent=None, key="c"):
    from apps.canopy_sessions.models import Session

    sess = Session.objects.create(agent=agent, workspace=fleet["dimagi"], created_by=fleet["jj"])
    return _turn(key, chat_session=sess, enqueued_by=fleet["jj"])


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_an_undedicated_runner_is_unrestricted(fleet):
    assert routing.dedicated_agent_slugs(fleet["box"]) is None
    assert routing.serves_refusal(fleet["box"], "ace") == ""


def test_a_dedicated_runner_claims_only_its_agents(fleet):
    box = _dedicate(fleet["box"], fleet["labsdata"])
    t_ace = _turn("ace", agent=fleet["ace"])
    assert routing.cannot_serve(box, "ace") == "cloud-zdr-1 runs only labsdata"
    assert claim.claim_next_turn(box) is None
    t_lab = _turn("labsdata", agent=fleet["labsdata"])
    assert claim.claim_next_turn(box).pk == t_lab.pk
    assert Turn.objects.get(pk=t_ace.pk).status == Turn.QUEUED
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(fleet["jj"])}
    assert rows[str(t_ace.pk)]["kind"] == "config"
    assert "runs only labsdata" in rows[str(t_ace.pk)]["reason"]


def test_a_dedicated_runner_takes_its_agents_chats_and_nothing_agentless(fleet):
    box = _dedicate(fleet["box"], fleet["labsdata"])
    repo = _turn("repo", project="canopy-web", workspace=fleet["dimagi"])
    agentless = _chat(fleet, key="agentless")
    pinned = _turn("pinned", agent=fleet["ace"], pinned_runner=box)
    assert claim.claim_next_turn(box) is None
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(fleet["jj"])}
    for t in (repo, agentless, pinned):
        assert "runs only labsdata" in rows[str(t.pk)]["reason"], rows[str(t.pk)]
    lab_chat = _chat(fleet, agent=fleet["labsdata"], key="lab")
    assert claim.turn_reach(Turn.objects.get(pk=lab_chat.pk)).kind == claim.LIVE
    assert claim.claim_next_turn(box).pk == lab_chat.pk


def test_the_vault_and_box_preparation_read_the_same_rule(fleet):
    box = _dedicate(fleet["box"], fleet["labsdata"])
    assert routing.runner_routes_agent(box, fleet["labsdata"])
    assert not routing.runner_routes_agent(box, fleet["ace"])
    assert [a.slug for a in routing.agents_routed_to(box)] == ["labsdata"]


def test_the_api_sets_shows_and_clears_the_list(fleet):
    box, c = fleet["box"], _client(fleet["jj"])
    url = f"/api/harness/runners/{box.pk}/only-agents"
    resp = c.put(url, {"agents": ["labsdata"]}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["only_agents"] == ["labsdata"]
    listed = {r["name"]: r for r in c.get("/api/harness/runners/").json()}
    assert listed["cloud-zdr-1"]["only_agents"] == ["labsdata"]

    resp = c.put(url, {"agents": ["labsdata", "nope"]}, content_type="application/json")
    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]
    assert routing.dedicated_agent_slugs(Runner.objects.get(pk=box.pk)) == {"labsdata"}

    resp = c.put(url, {"agents": []}, content_type="application/json")
    assert resp.status_code == 200 and resp.json()["only_agents"] == []
    assert routing.dedicated_agent_slugs(Runner.objects.get(pk=box.pk)) is None


def test_only_the_owner_or_a_runner_admin_may_set_it(fleet):
    box = fleet["box"]
    mia = get_user_model().objects.create_user(username="mia", email="mia@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["dimagi"], user=mia,
                                       role=WorkspaceMembership.EDITOR)
    url = f"/api/harness/runners/{box.pk}/only-agents"
    resp = _client(mia).put(url, {"agents": ["labsdata"]}, content_type="application/json")
    assert resp.status_code in (403, 404), resp.content
    assert not box.only_agents.exists()
    RunnerAdmin.objects.create(runner=box, user=mia, granted_by=fleet["jj"])
    resp = _client(mia).put(url, {"agents": ["labsdata"]}, content_type="application/json")
    assert resp.status_code == 200, resp.content


def test_an_agent_outside_the_boxs_tenant_is_refused(fleet):
    other = Workspace.objects.create(slug="marshellis", display_name="M", created_by=fleet["jj"])
    WorkspaceMembership.objects.create(workspace=other, user=fleet["jj"],
                                       role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="bailey", name="Bailey", workspace=other, owner=fleet["jj"])
    resp = _client(fleet["jj"]).put(f"/api/harness/runners/{fleet['box'].pk}/only-agents",
                                    {"agents": ["bailey"]}, content_type="application/json")
    assert resp.status_code == 422
    assert "bailey" in resp.json()["detail"]


def test_the_choices_are_the_agents_the_box_could_hold(fleet):
    other = Workspace.objects.create(slug="marshellis", display_name="M", created_by=fleet["jj"])
    WorkspaceMembership.objects.create(workspace=other, user=fleet["jj"],
                                       role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="bailey", name="Bailey", workspace=other, owner=fleet["jj"])
    fleet["box"].only_agents.set([fleet["labsdata"]])
    resp = _client(fleet["jj"]).get(f"/api/harness/runners/{fleet['box'].pk}/only-agents")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["agents"] == ["labsdata"]
    # bailey's workspace is not one the box serves.
    assert [c["slug"] for c in body["choices"]] == ["ace", "labsdata"]


# ---- "directed only" is a person's setting -----------------------------------------
def test_an_admin_can_put_a_directed_box_back_on_routed_work(fleet):
    Runner.objects.filter(pk=fleet["box"].pk).update(directed=True)
    box = _dedicate(fleet["box"], fleet["labsdata"])
    t = _turn("lab", agent=fleet["labsdata"])
    assert box.directed_only() and claim.claim_next_turn(box) is None

    c = _client(fleet["jj"])
    url = f"/api/harness/runners/{box.pk}/directed"
    resp = c.put(url, {"directed": False}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["directed"] is False
    box = Runner.objects.get(pk=box.pk)
    assert claim.turn_reach(Turn.objects.get(pk=t.pk)).kind == claim.LIVE
    assert claim.claim_next_turn(box).pk == t.pk

    resp = c.put(url, {"directed": True}, content_type="application/json")
    assert resp.json()["directed"] is True


def test_a_box_cannot_declare_itself_directed(fleet):
    """What the box says is not read: only the setting decides."""
    Runner.objects.filter(pk=fleet["box"].pk).update(
        capabilities={**fleet["box"].capabilities, "directed": True})
    assert not Runner.objects.get(pk=fleet["box"].pk).directed_only()


def test_a_private_runner_stays_directed(fleet):
    Runner.objects.filter(pk=fleet["box"].pk).update(private=True, directed=False)
    assert Runner.objects.get(pk=fleet["box"].pk).directed_only()
