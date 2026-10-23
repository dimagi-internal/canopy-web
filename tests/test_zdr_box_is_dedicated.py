"""A ZDR box holds only agents with a `data: phi` capability — whoever owns it
(spec 2026-10-22-phi-agents-on-zdr-runners §1, "A dedicated box").

The owner of a ZDR box is usually an admin of many agents; without this, routing
could place ACE's full-profile turn, its secrets and its GitHub token on the box
that holds PHI conversations. `runner_may_hold_agent` is the one predicate
behind claim, pins, credentials and routing writes, so the rule lives there.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.agents.services import runner_may_hold_agent
from apps.harness.runner_requirements import is_zdr_box
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

PHI_IFACE = {"capabilities": {"data": {
    "callers": ["contact", "member"], "sites": ["connect-labs"], "data": "phi",
    "tools": ["Read", "Grep", "Glob", "mcp__*canopy-web__current_page"]}}}


@pytest.fixture
def w():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    labsdata = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=jj,
                                    interface=parse(PHI_IFACE))
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    box = Runner.objects.create(name="cloud-zdr-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                capabilities={"sessions": True})
    return {"ws": ws, "labsdata": labsdata, "ace": ace, "box": box}


def _declare(box):
    RunnerFlag.objects.create(runner=box, flag="zdr")


def _reports(box):
    Runner.objects.filter(pk=box.pk).update(capabilities={"sessions": True, "data_mode": "zdr"})
    box.refresh_from_db()


def test_an_ordinary_box_holds_either_agent(w):
    assert not is_zdr_box(w["box"])
    assert runner_may_hold_agent(w["box"], w["ace"])
    assert runner_may_hold_agent(w["box"], w["labsdata"])


@pytest.mark.parametrize("make_zdr", [_declare, _reports, lambda b: (_declare(b), _reports(b))],
                         ids=["declared", "reported", "both"])
def test_a_zdr_box_holds_only_the_phi_agent(w, make_zdr):
    make_zdr(w["box"])
    box = Runner.objects.get(pk=w["box"].pk)

    assert is_zdr_box(box)
    assert not runner_may_hold_agent(box, w["ace"])
    assert runner_may_hold_agent(box, w["labsdata"])


def test_a_zdr_box_does_not_claim_another_agents_turn(w):
    _declare(w["box"])
    _reports(w["box"])
    RunnerAssignment.objects.create(agent=w["ace"], runner=w["box"], rank=0)
    turn = Turn.objects.create(agent=w["ace"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key="ace-1", routing=Turn.ANY)

    assert claim.claim_next_turn(w["box"]) is None

    # The same box, no longer ZDR, takes it — so the refusal above is dedication.
    RunnerFlag.objects.filter(runner=w["box"]).delete()
    Runner.objects.filter(pk=w["box"].pk).update(capabilities={"sessions": True})
    claimed = claim.claim_next_turn(Runner.objects.get(pk=w["box"].pk))
    assert claimed is not None and claimed.pk == turn.pk


def test_withdrawing_phi_takes_the_agent_off_the_box(w):
    _declare(w["box"])
    w["labsdata"].interface = parse({"capabilities": {"data": {
        "callers": ["member"], "sites": ["connect-labs"], "tools": ["Read"]}}})
    w["labsdata"].save(update_fields=["interface"])

    assert not runner_may_hold_agent(w["box"], w["labsdata"])
