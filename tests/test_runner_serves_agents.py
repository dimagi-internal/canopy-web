"""Which agents a box runs: ONE rule, shared by the claim, the vault and the box's
own refresh (canopy-web#1563, fizzy 2026-10-17).

The box's credential refresh used to walk every agent its owner's token could
list, and the vault accepted a box by a looser rule than claiming used — it
counted disabled rows and ignored enrollment and the hold check. So a box that
could never claim fizzy asked for fizzy's vault every 30 minutes, and once fizzy
became zero-knowledge every refusal emailed Shayoni. Now:

- `routing.runner_routes_agent` is the claim's own static gates;
- the vault accepts that, a box holding a live turn of the agent, or the owner's
  own laptop (`device_keys.runner_serves_agent`);
- a box prepares exactly the agents it runs (`GET /runners/{id}/agents`);
- and the parity test below holds the rule to what `claim_next_turn` does.
"""
from __future__ import annotations

import datetime as dt
import itertools

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import device_keys, routing
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, Turn, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name, ws, kind=Runner.CLOUD):
    # `workspace` enrolls the box there, as pairing does.
    return Runner.objects.create(name=name, kind=kind, host=name, owner=owner, workspace=ws,
                                 status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                 capabilities={})


@pytest.fixture
def fleet():
    jj, shayoni, ed = _user("jj"), _user("shayoni"), _user("ed")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    family = Workspace.objects.create(slug="family", display_name="Family", created_by=jj)
    for ws in (dimagi, family):
        WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    for u in (shayoni, ed):  # editors: may run work in Dimagi, admins of no agent
        WorkspaceMembership.objects.create(workspace=dimagi, user=u,
                                           role=WorkspaceMembership.EDITOR)
    cloud = _runner(jj, "cloud-ec2-2", dimagi)
    laptop = _runner(shayoni, "shayoni-win-cdp", dimagi, kind=Runner.EMDASH)
    jj_laptop = _runner(jj, "jj-mbp", dimagi, kind=Runner.EMDASH)
    eds_box = _runner(ed, "ed-box", dimagi)
    WorkspaceRunnerOrder.objects.create(workspace=dimagi, runner=cloud, rank=0)

    def agent(slug, owner, ws=dimagi):
        return Agent.objects.create(slug=slug, name=slug, workspace=ws, owner=owner)

    a = {
        "hal": agent("hal", jj),                 # follows the Dimagi order
        "eva": agent("eva", shayoni),            # likewise; jj owns Dimagi, so may hold it
        "fizzy": agent("fizzy", shayoni),        # its own order: Shayoni's laptop only
        "off": agent("off", jj),                 # assigned to the cloud box, switched off
        "emailer": agent("emailer", jj),         # email turns go to the cloud box by rule
        "mine": agent("mine", jj),               # jj's, but routed only to jj's laptop
        "nanny": agent("nanny", jj, family),     # a separate root the box is not enrolled in
        "eds": agent("eds", jj),                 # routed to Ed's box; Ed may not hold it
    }
    RunnerAssignment.objects.create(agent=a["fizzy"], runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a["off"], runner=cloud, rank=0, enabled=False)
    RunnerAssignment.objects.create(agent=a["off"], runner=jj_laptop, rank=1)
    RunnerAssignment.objects.create(agent=a["emailer"], runner=jj_laptop, rank=0)
    RunnerAssignment.objects.create(agent=a["emailer"], runner=cloud, rank=0,
                                    source=Turn.ORIGIN_EMAIL)
    RunnerAssignment.objects.create(agent=a["mine"], runner=jj_laptop, rank=0)
    RunnerAssignment.objects.create(agent=a["nanny"], runner=cloud, rank=0)
    RunnerAssignment.objects.create(agent=a["eds"], runner=eds_box, rank=0)
    return {"cloud": cloud, "laptop": laptop, "jj_laptop": jj_laptop, "eds_box": eds_box,
            "agents": a}


def _agents(runner, as_user=None):
    c = Client()
    c.force_login(as_user or runner.owner)
    return c.get(f"/api/harness/runners/{runner.pk}/agents")


def test_the_cloud_box_runs_only_what_routes_to_it(fleet):
    r = _agents(fleet["cloud"])
    assert r.status_code == 200, r.content
    # hal, eva: the workspace order. emailer: an email rule. NOT fizzy (Shayoni's
    # laptop), off (disabled), mine (jj owns it, but it routes to jj's laptop),
    # nanny (not enrolled there), eds (another box).
    assert r.json() == ["emailer", "eva", "hal"]


def test_a_disabled_row_unenrolled_tenant_or_unholdable_owner_does_not_route(fleet):
    a, cloud = fleet["agents"], fleet["cloud"]
    assert not routing.runner_routes_agent(cloud, a["off"])
    assert not routing.runner_routes_agent(cloud, a["nanny"])
    assert not routing.runner_routes_agent(fleet["eds_box"], a["eds"])


def test_the_vault_rule_is_the_routing_rule_plus_two_named_exceptions(fleet):
    a, cloud = fleet["agents"], fleet["cloud"]
    # The owner's own CLOUD box is no reason on its own; the owner's own laptop is.
    assert not device_keys.runner_serves_agent(cloud, a["mine"])
    assert device_keys.runner_serves_agent(fleet["jj_laptop"], a["hal"])
    # A box holding a live turn of the agent (a pinned dispatch) may read it for
    # that turn, but does not therefore prepare the agent.
    assert not device_keys.runner_serves_agent(cloud, a["fizzy"])
    Turn.objects.create(agent=a["fizzy"], origin=Turn.ORIGIN_API, idempotency_key="pin",
                        status=Turn.RUNNING, claimed_by=cloud, pinned_runner=cloud)
    assert device_keys.runner_serves_agent(cloud, a["fizzy"])
    assert "fizzy" not in _agents(cloud).json()


def test_the_rule_is_what_claiming_does(fleet):
    """PARITY: for every box and agent, "routes" == "the claim hands it a turn of
    that agent", with the cascade taken out (the turn is past the grace). This is
    the drift that let the vault and the box's roster disagree with claiming."""
    a = fleet["agents"]
    boxes = [fleet[k] for k in ("cloud", "laptop", "jj_laptop", "eds_box")]
    for box in boxes:  # laptops claim only agents whose repo they declare
        if box.kind == Runner.EMDASH:
            box.capabilities = {"projects": list(a)}
            box.save(update_fields=["capabilities"])
    for n, (box, agent) in enumerate(itertools.product(boxes, a.values())):
        origin = Turn.ORIGIN_EMAIL if agent.slug == "emailer" else Turn.ORIGIN_API
        turn = Turn.objects.create(agent=agent, origin=origin, idempotency_key=f"p{n}",
                                   routing=Turn.ANY)
        Turn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=3600))
        got = claim.claim_next_turn(box)
        claimed = got is not None and got.pk == turn.pk
        Turn.objects.filter(pk=turn.pk).delete()
        expected = routing.runner_routes_agent(box, agent)
        if agent.slug == "emailer" and box == fleet["cloud"]:
            assert expected and claimed, "an email rule routes the agent to the box"
        assert claimed == expected, (box.name, agent.slug, claimed, expected)


def test_a_retired_box_runs_nothing(fleet):
    cloud = fleet["cloud"]
    Runner.objects.filter(pk=cloud.pk).update(status=Runner.RETIRED)
    cloud.refresh_from_db()
    assert list(routing.agents_routed_to(cloud)) == []
    assert list(device_keys.agents_served(cloud)) == []


def test_someone_elses_box_is_not_yours_to_ask_about(fleet):
    assert _agents(fleet["cloud"], as_user=fleet["laptop"].owner).status_code == 404
