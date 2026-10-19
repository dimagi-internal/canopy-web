"""A cloud box runs one runner per isolation unit (spec 2026-10-19 §3).

A unit is a workspace tree or a single agent, each its own Unix user with its own
keys. Two refusals keep routing inside that, both declared by the box about
itself (`routing.unit_refusal`, part of `routing.cannot_serve`):

  - an AGENT unit (`capabilities.unit = {"kind": "agent", "slug": S}`) runs only S;
  - when an agent has a unit of its own on a machine (`Runner.machine_id`), no
    other runner on that machine runs it, so its secrets stay in its unit.

The claim, the unclaimable report, the vault's "does this box run the agent"
(`runner_routes_agent`) and the routing writes all ask the same function.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import routing, services
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey, Turn, WorkspaceRunnerOrder
from apps.harness.runners import backfill_machine_ids, set_runner_machine
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ROLE = "arn:aws:iam::111122223333:role/cloud-ec2-2-RunnerRole"


def _runner(owner, name, ws, *, unit=None, machine="", kind=Runner.CLOUD, **caps):
    if unit is not None:
        caps["unit"] = unit
    return Runner.objects.create(name=name, kind=kind, host=name, owner=owner, workspace=ws,
                                 status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                 capabilities=caps, machine_id=machine)


@pytest.fixture
def box():
    """One machine (cloud-ec2-2) with two units: the dimagi tree, and eva on her own."""
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    tree = _runner(jj, "cloud-ec2-2-dimagi", dimagi, unit={"kind": "tree", "root": "dimagi"},
                   machine=ROLE)
    eva_unit = _runner(jj, "cloud-ec2-2-agent-eva", dimagi, unit={"kind": "agent", "slug": "eva"},
                       machine=ROLE)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=jj)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=dimagi, owner=jj)
    return {"jj": jj, "dimagi": dimagi, "tree": tree, "eva_unit": eva_unit, "eva": eva, "ace": ace}


def _turn(agent, key, *, age=3600, **kw):
    t = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key,
                            routing=Turn.ANY, prompt="hi", **kw)
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age))
    return t


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ---- the refusals themselves ---------------------------------------------------------
def test_cannot_serve_is_unchanged_for_runners_with_no_unit_and_no_machine(box):
    plain = _runner(box["jj"], "jj-cloud", box["dimagi"])
    assert routing.unit_refusal(plain, "eva") == ""
    assert routing.cannot_serve(plain, "eva") == ""
    assert routing.cannot_serve(plain, "ace") == ""
    # A tree unit on a machine with no agent unit refuses nothing either.
    other = _runner(box["jj"], "cloud-ec2-9", box["dimagi"],
                    unit={"kind": "tree", "root": "dimagi"}, machine=ROLE.replace("2", "9"))
    assert routing.cannot_serve(other, "eva") == ""


def test_the_two_refusals_and_their_reasons(box):
    tree, eva_unit = box["tree"], box["eva_unit"]
    assert routing.cannot_serve(eva_unit, "eva") == ""
    assert routing.cannot_serve(eva_unit, "ace") == "cloud-ec2-2-agent-eva is the agent unit for eva"
    assert routing.cannot_serve(tree, "ace") == ""
    assert routing.cannot_serve(tree, "eva") == (
        "eva has its own unit on this machine (cloud-ec2-2-agent-eva)")


def test_a_retired_agent_unit_releases_its_agent(box):
    Runner.objects.filter(pk=box["eva_unit"].pk).update(status=Runner.RETIRED)
    tree = Runner.objects.get(pk=box["tree"].pk)
    assert routing.cannot_serve(tree, "eva") == ""


# ---- claim parity ----------------------------------------------------------------------
def test_an_agent_unit_claims_only_its_agent(box):
    eva_unit, ace, eva = box["eva_unit"], box["ace"], box["eva"]
    # Rows written straight to the table, past the routing-write gate.
    RunnerAssignment.objects.create(agent=ace, runner=eva_unit, rank=0)
    RunnerAssignment.objects.create(agent=eva, runner=eva_unit, rank=0)
    t_ace = _turn(ace, "ace")
    assert services.claim_next_turn(eva_unit) is None
    t_eva = _turn(eva, "eva")
    claimed = services.claim_next_turn(eva_unit)
    assert claimed is not None and claimed.pk == t_eva.pk
    assert Turn.objects.get(pk=t_ace.pk).status == Turn.QUEUED

    rows = {r["turn_id"]: r for r in services.unclaimable_queued_turns(box["jj"])}
    assert rows[str(t_ace.pk)]["kind"] == "config"
    assert "agent unit for eva" in rows[str(t_ace.pk)]["reason"]


def test_an_agent_with_its_own_unit_is_claimed_by_no_other_unit_on_the_machine(box):
    tree, eva_unit, eva = box["tree"], box["eva_unit"], box["eva"]
    RunnerAssignment.objects.create(agent=eva, runner=tree, rank=0)
    t = _turn(eva, "eva")
    assert services.claim_next_turn(tree) is None
    rows = {r["turn_id"]: r for r in services.unclaimable_queued_turns(box["jj"])}
    assert "eva has its own unit on this machine" in rows[str(t.pk)]["reason"]

    RunnerAssignment.objects.create(agent=eva, runner=eva_unit, rank=1)
    assert services.claim_next_turn(eva_unit).pk == t.pk


def test_a_workspace_order_naming_both_units_routes_each_agent_to_its_own(box):
    tree, eva_unit = box["tree"], box["eva_unit"]
    WorkspaceRunnerOrder.objects.create(workspace=box["dimagi"], runner=eva_unit, rank=0)
    WorkspaceRunnerOrder.objects.create(workspace=box["dimagi"], runner=tree, rank=1)
    t_ace, t_eva = _turn(box["ace"], "ace", age=0), _turn(box["eva"], "eva", age=0)
    # The eva unit is not a better-ranked blocker for ace: it is not in ace's list.
    assert services.claim_next_turn(tree).pk == t_ace.pk
    assert services.claim_next_turn(tree) is None
    assert services.claim_next_turn(eva_unit).pk == t_eva.pk


def test_a_pin_does_not_get_past_the_unit(box):
    eva_unit, tree = box["eva_unit"], box["tree"]
    t_ace = _turn(box["ace"], "ace", pinned_runner=eva_unit)
    t_eva = _turn(box["eva"], "eva", pinned_runner=tree)
    assert services.claim_next_turn(eva_unit) is None
    assert services.claim_next_turn(tree) is None
    rows = {r["turn_id"]: r for r in services.unclaimable_queued_turns(box["jj"])}
    assert "agent unit for eva" in rows[str(t_ace.pk)]["reason"]
    assert "eva has its own unit on this machine" in rows[str(t_eva.pk)]["reason"]


def test_the_vault_reads_the_same_rule(box):
    from apps.harness.device_keys import runner_serves_agent

    tree, eva_unit, eva, ace = box["tree"], box["eva_unit"], box["eva"], box["ace"]
    RunnerAssignment.objects.create(agent=eva, runner=tree, rank=0)
    RunnerAssignment.objects.create(agent=ace, runner=eva_unit, rank=0)
    RunnerAssignment.objects.create(agent=eva, runner=eva_unit, rank=0)
    assert not routing.runner_routes_agent(tree, eva)
    assert not routing.runner_routes_agent(eva_unit, ace)
    assert routing.runner_routes_agent(eva_unit, eva)
    assert not runner_serves_agent(tree, eva)
    assert [a.slug for a in routing.agents_routed_to(eva_unit)] == ["eva"]
    assert "eva" not in {a.slug for a in routing.agents_routed_to(tree)}


# ---- routing writes --------------------------------------------------------------------
def test_routing_writes_that_cross_a_unit_are_refused(box):
    c = _client(box["jj"])
    resp = c.put("/api/agents/ace/runners", {"runner_ids": [str(box["eva_unit"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 422, resp.content
    assert "agent unit for eva" in resp.json()["detail"]
    resp = c.put("/api/agents/eva/runners", {"runner_ids": [str(box["tree"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 422
    assert "eva has its own unit on this machine" in resp.json()["detail"]
    assert not RunnerAssignment.objects.exists()

    resp = c.put("/api/agents/eva/runners", {"runner_ids": [str(box["eva_unit"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 200, resp.content
    resp = c.put("/api/agents/ace/runners", {"runner_ids": [str(box["tree"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 200, resp.content


# ---- the topology groups a box's units -------------------------------------------------
def test_the_topology_lists_a_boxs_units_together(box):
    _runner(box["jj"], "a-laptop", box["dimagi"], kind=Runner.EMDASH)
    _runner(box["jj"], "zz-other-box", box["dimagi"], machine="arn:aws:iam::111122223333:role/zz")
    rows = _client(box["jj"]).get("/api/workspaces/dimagi/runner-topology").json()["runners"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["cloud-ec2-2-dimagi"]["machine"] == "cloud-ec2-2-RunnerRole"
    assert by_name["cloud-ec2-2-agent-eva"]["machine"] == "cloud-ec2-2-RunnerRole"
    assert by_name["cloud-ec2-2-dimagi"]["unit"] == "tree:dimagi"
    assert by_name["cloud-ec2-2-agent-eva"]["unit"] == "agent:eva"
    assert by_name["a-laptop"]["machine"] == "" and by_name["a-laptop"]["unit"] == ""
    names = [r["name"] for r in rows]
    i, j = names.index("cloud-ec2-2-agent-eva"), names.index("cloud-ec2-2-dimagi")
    assert abs(i - j) == 1


# ---- the machine id ---------------------------------------------------------------------
def test_set_runner_machine_canonicalises_and_is_idempotent(box):
    r = _runner(box["jj"], "fresh", box["dimagi"])
    assert set_runner_machine(r, "arn:aws:sts::111122223333:assumed-role/fresh-role/i-0abc")
    assert Runner.objects.get(pk=r.pk).machine_id == "arn:aws:iam::111122223333:role/fresh-role"
    assert not set_runner_machine(r, "arn:aws:iam::111122223333:role/fresh-role")


def test_backfill_reads_active_aws_iam_keys_only(box):
    from apps.harness.aws_iam_proof import role_jkt

    active = _runner(box["jj"], "active", box["dimagi"])
    pending = _runner(box["jj"], "pending", box["dimagi"])
    for r, status, arn in ((active, RunnerDeviceKey.ACTIVE, "arn:aws:iam::111122223333:role/a"),
                           (pending, RunnerDeviceKey.PENDING, "arn:aws:iam::111122223333:role/p")):
        RunnerDeviceKey.objects.create(runner=r, kind=RunnerDeviceKey.AWS_IAM, role_arn=arn,
                                       jkt=role_jkt(arn), status=status)
    assert backfill_machine_ids(Runner, RunnerDeviceKey) == 1
    assert Runner.objects.get(pk=active.pk).machine_id == "arn:aws:iam::111122223333:role/a"
    assert Runner.objects.get(pk=pending.pk).machine_id == ""
    assert backfill_machine_ids(Runner, RunnerDeviceKey) == 0  # already set


def test_approving_an_aws_iam_key_records_the_machine(box):
    from apps.harness.aws_iam_proof import role_jkt
    from apps.passkeys.testing import grant_step_up

    r = _runner(box["jj"], "new-box", box["dimagi"])
    key = RunnerDeviceKey.objects.create(runner=r, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
                                         jkt=role_jkt(ROLE), status=RunnerDeviceKey.PENDING)
    c = _client(box["jj"])
    grant_step_up(c)
    resp = c.post(f"/api/runner-device-keys/{key.pk}/approve")
    assert resp.status_code == 200, resp.content
    assert Runner.objects.get(pk=r.pk).machine_id == ROLE
