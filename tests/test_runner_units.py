"""A cloud machine can run several runners (spec 2026-10-19 §3).

Each runner serves a workspace tree or a single agent, each its own Unix user with
its own keys. Two refusals keep routing inside that, both declared by the box about
itself (`routing.unit_refusal`, part of `routing.cannot_serve`):

  - one AGENT's runner (`capabilities.unit = {"kind": "agent", "slug": S}`) runs only S;
  - when an agent has a runner of its own on a machine (`Runner.machine_id`), no
    other runner on that machine runs it, so its secrets stay with that runner.

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
from apps.harness import routing
from apps.harness import claim, runners as harness_runners
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
    """One machine (cloud-ec2-2) with two runners: the dimagi tree, and eva on her own."""
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
    # A tree's runner on a machine with no agent's runner refuses nothing either.
    other = _runner(box["jj"], "cloud-ec2-9", box["dimagi"],
                    unit={"kind": "tree", "root": "dimagi"}, machine=ROLE.replace("2", "9"))
    assert routing.cannot_serve(other, "eva") == ""


def test_the_two_refusals_and_their_reasons(box):
    tree, eva_unit = box["tree"], box["eva_unit"]
    assert routing.cannot_serve(eva_unit, "eva") == ""
    assert routing.cannot_serve(eva_unit, "ace") == "cloud-ec2-2-agent-eva is the runner for agent eva"
    assert routing.cannot_serve(tree, "ace") == ""
    assert routing.cannot_serve(tree, "eva") == (
        "eva has its own runner on this machine (cloud-ec2-2-agent-eva)")


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
    assert claim.claim_next_turn(eva_unit) is None
    t_eva = _turn(eva, "eva")
    claimed = claim.claim_next_turn(eva_unit)
    assert claimed is not None and claimed.pk == t_eva.pk
    assert Turn.objects.get(pk=t_ace.pk).status == Turn.QUEUED

    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(box["jj"])}
    assert rows[str(t_ace.pk)]["kind"] == "config"
    assert "runner for agent eva" in rows[str(t_ace.pk)]["reason"]


def test_an_agent_with_its_own_unit_is_claimed_by_no_other_unit_on_the_machine(box):
    tree, eva_unit, eva = box["tree"], box["eva_unit"], box["eva"]
    RunnerAssignment.objects.create(agent=eva, runner=tree, rank=0)
    t = _turn(eva, "eva")
    assert claim.claim_next_turn(tree) is None
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(box["jj"])}
    assert "eva has its own runner on this machine" in rows[str(t.pk)]["reason"]

    RunnerAssignment.objects.create(agent=eva, runner=eva_unit, rank=1)
    assert claim.claim_next_turn(eva_unit).pk == t.pk


def test_a_workspace_order_naming_both_units_routes_each_agent_to_its_own(box):
    tree, eva_unit = box["tree"], box["eva_unit"]
    WorkspaceRunnerOrder.objects.create(workspace=box["dimagi"], runner=eva_unit, rank=0)
    WorkspaceRunnerOrder.objects.create(workspace=box["dimagi"], runner=tree, rank=1)
    t_ace, t_eva = _turn(box["ace"], "ace", age=0), _turn(box["eva"], "eva", age=0)
    # The eva runner is not a better-ranked blocker for ace: it is not in ace's list.
    assert claim.claim_next_turn(tree).pk == t_ace.pk
    assert claim.claim_next_turn(tree) is None
    assert claim.claim_next_turn(eva_unit).pk == t_eva.pk


def test_a_pin_does_not_get_past_the_unit(box):
    eva_unit, tree = box["eva_unit"], box["tree"]
    t_ace = _turn(box["ace"], "ace", pinned_runner=eva_unit)
    t_eva = _turn(box["eva"], "eva", pinned_runner=tree)
    assert claim.claim_next_turn(eva_unit) is None
    assert claim.claim_next_turn(tree) is None
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(box["jj"])}
    assert "runner for agent eva" in rows[str(t_ace.pk)]["reason"]
    assert "eva has its own runner on this machine" in rows[str(t_eva.pk)]["reason"]


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
    assert "runner for agent eva" in resp.json()["detail"]
    resp = c.put("/api/agents/eva/runners", {"runner_ids": [str(box["tree"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 422
    assert "eva has its own runner on this machine" in resp.json()["detail"]
    assert not RunnerAssignment.objects.exists()

    resp = c.put("/api/agents/eva/runners", {"runner_ids": [str(box["eva_unit"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 200, resp.content
    resp = c.put("/api/agents/ace/runners", {"runner_ids": [str(box["tree"].pk)]},
                 content_type="application/json")
    assert resp.status_code == 200, resp.content


# ---- the topology groups a machine's runners ---------------------------------------------
def test_the_topology_lists_a_boxs_units_together(box):
    _runner(box["jj"], "a-laptop", box["dimagi"], kind=Runner.EMDASH)
    _runner(box["jj"], "zz-other-box", box["dimagi"], machine="arn:aws:iam::111122223333:role/zz")
    rows = _client(box["jj"]).get("/api/workspaces/dimagi/runner-topology").json()["runners"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["cloud-ec2-2-dimagi"]["machine"] == "cloud-ec2-2"
    assert by_name["cloud-ec2-2-agent-eva"]["machine"] == "cloud-ec2-2"
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


# ---- an agent's runner takes nothing that does not act as its agent ----------------------
def _chat(box, agent=None, bound_to=None, key="c"):
    from apps.canopy_sessions.models import RunnerBinding, Session

    sess = Session.objects.create(agent=agent, workspace=box["dimagi"], created_by=box["jj"])
    if bound_to is not None:
        RunnerBinding.objects.create(session=sess, runner=bound_to, session_key=f"k-{key}")
    # Sent by jj, who administers every box here (a project chat with nobody
    # behind it and no workspace order goes nowhere).
    t = Turn.objects.create(chat_session=sess, origin=Turn.ORIGIN_API, idempotency_key=key,
                            routing=Turn.ANY, prompt="hi", enqueued_by=box["jj"])
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(hours=1))
    return t


def _session_capable(*runners):
    for r in runners:
        Runner.objects.filter(pk=r.pk).update(
            capabilities={**r.capabilities, "sessions": True, "projects": ["canopy-web"]})
        r.refresh_from_db()


def test_an_agent_unit_takes_no_repo_turn_and_no_agentless_chat(box):
    eva_unit = box["eva_unit"]
    _session_capable(eva_unit)
    repo = Turn.objects.create(project="canopy-web", workspace=box["dimagi"], origin=Turn.ORIGIN_API,
                               idempotency_key="repo", routing=Turn.ANY, prompt="fix it")
    Turn.objects.filter(pk=repo.pk).update(created_at=timezone.now() - dt.timedelta(hours=1))
    chat = _chat(box)
    pinned = Turn.objects.create(project="other-repo", workspace=box["dimagi"],
                                 origin=Turn.ORIGIN_API, idempotency_key="pinned",
                                 routing=Turn.ANY, prompt="drill", pinned_runner=eva_unit)
    Turn.objects.filter(pk=pinned.pk).update(created_at=timezone.now() - dt.timedelta(hours=1))
    assert claim.claim_next_turn(eva_unit) is None
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(box["jj"])}
    for t in (repo, chat, pinned):
        assert "runner for agent eva" in rows[str(t.pk)]["reason"], rows[str(t.pk)]
    # Its own agent's chat it takes.
    own = _chat(box, agent=box["eva"], key="eva-chat")
    RunnerAssignment.objects.create(agent=box["eva"], runner=eva_unit, rank=0)
    assert claim.claim_next_turn(eva_unit).pk == own.pk


def test_a_tree_unit_still_takes_agentless_chats(box):
    tree = box["tree"]
    _session_capable(tree)
    chat = _chat(box)
    assert claim.claim_next_turn(tree).pk == chat.pk


def test_a_bound_chat_the_unit_refuses_reads_the_same_everywhere(box):
    """The binding short-circuit must not report a chat as live that the claim
    refuses: an ace chat bound to the eva runner, an eva chat bound to the tree."""
    from apps.harness.claim import turn_reach

    tree, eva_unit = box["tree"], box["eva_unit"]
    _session_capable(tree, eva_unit)
    ace_chat = _chat(box, agent=box["ace"], bound_to=eva_unit, key="ace")
    eva_chat = _chat(box, agent=box["eva"], bound_to=tree, key="eva")
    assert claim.claim_next_turn(eva_unit) is None
    assert claim.claim_next_turn(tree) is None
    assert turn_reach(Turn.objects.get(pk=ace_chat.pk)).kind != "live"
    assert turn_reach(Turn.objects.get(pk=eva_chat.pk)).kind != "live"
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(box["jj"])}
    assert "runner for agent eva" in rows[str(ace_chat.pk)]["reason"]
    assert "eva has its own runner on this machine" in rows[str(eva_chat.pk)]["reason"]


# ---- what a caller learns of a runner's trees -------------------------------------------
def test_every_runner_route_stamps_roots_for_its_caller(box):
    """A non-owner runner admin learns only the trees they are a member in —
    from the fleet list and from every other route that returns the runner."""
    from apps.harness.models import RunnerAdmin

    jj, dimagi = box["jj"], box["dimagi"]
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj,
                                       parent=dimagi)
    family = Workspace.objects.create(slug="marshellis", display_name="M", created_by=jj)
    WorkspaceMembership.objects.create(workspace=family, user=jj, role=WorkspaceMembership.OWNER)
    laptop = _runner(jj, "jj-mbp", dimagi, kind=Runner.EMDASH)
    harness_runners.enroll_runner(laptop, "marshellis")
    ada = get_user_model().objects.create_user(username="ada", email="ada@dimagi.com")
    WorkspaceMembership.objects.create(workspace=connect, user=ada, role=WorkspaceMembership.EDITOR)
    RunnerAdmin.objects.create(runner=laptop, user=ada, granted_by=jj)
    c = _client(ada)
    import time

    from apps.passkeys import stepup

    s = c.session  # declaring a flag needs a fresh passkey confirmation
    s[stepup.SESSION_KEY] = {"user": ada.pk, "until": time.time() + 60}
    s.save()

    flags = c.put(f"/api/harness/runners/{laptop.pk}/flags", {"flags": []},
                  content_type="application/json")
    assert flags.status_code == 200, flags.content
    # A member of only `connect` sees `dimagi`, the root of its tree — never the family's.
    assert flags.json()["roots"] == ["dimagi"] and flags.json()["spans_trees"] is True
    listed = {r["name"]: r for r in c.get("/api/harness/runners/").json()}
    assert listed["jj-mbp"]["roots"] == ["dimagi"]
    owner = _client(jj)
    s = owner.session
    s[stepup.SESSION_KEY] = {"user": jj.pk, "until": time.time() + 60}
    s.save()
    own = owner.put(f"/api/harness/runners/{laptop.pk}/flags", {"flags": []},
                    content_type="application/json").json()
    assert own["roots"] == ["dimagi", "marshellis"]


def test_the_fleet_list_reads_roots_in_constant_queries(box, monkeypatch):
    """Roots for the whole list come from one tree walk: what they cost the list
    does not grow with the number of runners, or of trees they serve."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.harness import api_runners as harness_api

    jj, dimagi = box["jj"], box["dimagi"]
    family = Workspace.objects.create(slug="marshellis", display_name="M", created_by=jj)
    WorkspaceMembership.objects.create(workspace=family, user=jj, role=WorkspaceMembership.OWNER)
    c = _client(jj)
    real = harness_api._with_roots

    def no_roots(request, runners):
        for r in ([runners] if isinstance(runners, Runner) else runners):
            r._enrolled_roots_cache = []
        return runners

    def cost():
        """Queries the list spends on roots: with them, minus without them."""
        out = []
        for stamp in (real, no_roots):
            monkeypatch.setattr(harness_api, "_with_roots", stamp)
            with CaptureQueriesContext(connection) as ctx:
                assert c.get("/api/harness/runners/").status_code == 200
            out.append(len(ctx.captured_queries))
        monkeypatch.setattr(harness_api, "_with_roots", real)
        return out[0] - out[1]

    cost()  # warm the session
    few = cost()
    for i in range(5):
        laptop = _runner(jj, f"laptop-{i}", dimagi, kind=Runner.EMDASH)
        harness_runners.enroll_runner(laptop, "marshellis")
    many = cost()
    assert many == few, (few, many)
    rows = {r["name"]: r for r in c.get("/api/harness/runners/").json()}
    assert rows["laptop-0"]["roots"] == ["dimagi", "marshellis"]
    assert rows["laptop-0"]["spans_trees"] is True


def test_a_runner_names_its_machine_without_the_role_suffix():
    r = Runner(machine_id="arn:aws:iam::111122223333:role/canopy-cloud-runner-2-RunnerRole-rcnZI7uy9JOR")
    assert r.machine_name() == "canopy-cloud-runner-2"
    assert Runner(machine_id="").machine_name() == ""
