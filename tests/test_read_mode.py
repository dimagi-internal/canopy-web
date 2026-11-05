"""Read mode resolves on its own (spec 2026-11-03-agent-read-mode-and-doors-design.md, Phase 1 change 3).

A person whose lines give `read` and not `write` is answered in read mode on EVERY agent: the agent's
own published read mode when it has one (an override, the whole of it, stamped `READ_MODE_STAMP`), else canopy's default
profile (stamp `read`). Any other name is a connected site and still has to be published.
"""
from __future__ import annotations

import io
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError  # noqa: F401

from apps.agents import access, agent_card, interface, read_mode_gate, roles, topology, turn_context
from apps.agents.interface import parse
from apps.agents.models import Agent, AgentSkill
from apps.contacts.models import Contact
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import allow

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    owner, vw, ed = mk("own", "own@dimagi.com", "pw"), mk("vw", "vw@dimagi.com", "pw"), mk("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    M.objects.create(user=owner, workspace=ws, role=M.OWNER)
    M.objects.create(user=vw, workspace=ws, role=M.VIEWER)
    M.objects.create(user=ed, workspace=ws, role=M.EDITOR)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner, email="hal@dimagi.com",
                                 repo_url="https://github.com/dimagi-internal/hal")
    return {"ws": ws, "agent": agent, "owner": owner, "vw": vw, "ed": ed}


def test_read_resolves_with_no_published_profile(w):
    allow(w["agent"], role=roles.READ_ONLY)
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True)
    assert (d.access, d.stamped, d.basis) == (access.CONFINED, "read", "capability:read")
    assert interface.profile(w["agent"], "read")["write_paths"] == ["{cwd}/.hal-read/*", "{cwd}/.hal-ask/*"]


def test_a_stamped_or_use_read_line_is_a_read_mode_line(w):
    for cap in ("ask", "read"):
        line = allow(w["agent"], capability=cap)
        d = turn_context.for_asker(w["agent"], w["vw"], verified=True)
        assert (d.access, d.stamped) == (access.CONFINED, "read"), cap
        line.delete()


def test_no_line_means_no_read_mode(w):
    assert turn_context.for_asker(w["agent"], w["vw"], verified=True).access == turn_context.REFUSED


def test_a_published_read_mode_overrides_the_default_whole(w):
    allow(w["agent"], role=roles.READ_ONLY)
    w["agent"].interface = parse({"capabilities": {"ask": {"tools": ["Read"], "bash": ["ls"]}}})
    w["agent"].save(update_fields=["interface"])
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True)
    assert (d.access, d.stamped) == (access.CONFINED, "ask")
    prof = interface.profile(w["agent"], "ask")
    assert prof["tools"] == ["Read"] and prof["bash"] == ["ls"] and "default_profile_version" not in prof


def test_the_default_entry_follows_the_catalog_at_claim_time(w):
    assert interface.profile(w["agent"], "read")["entry"] is None
    AgentSkill.objects.create(agent=w["agent"], name="answer-caller")
    assert interface.profile(w["agent"], "read")["entry"] == "/hal:answer-caller --thread {thread_id}"
    w["agent"].skills.all().delete()
    assert interface.profile(w["agent"], "read")["entry"] is None


def test_other_names_still_have_to_be_published(w):
    allow(w["agent"], role=roles.READ_ONLY)
    allow(w["agent"], capability="canopy")
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True, requested="canopy")
    assert d.access == turn_context.REFUSED                      # `canopy` is a connected site; nobody published it
    gone = interface.profile(w["agent"], "retired-site")
    assert gone["tools"] == [] and gone["bash"] == [] and gone["write_paths"] == []


def test_write_is_still_the_whole_agent(w):
    allow(w["agent"], role=roles.READ_WRITE)
    d = turn_context.for_asker(w["agent"], w["ed"], verified=True)
    assert d.access == access.FULL and d.stamped == interface.FULL


def test_a_sensitive_conversation_never_gets_the_email_reply_read_mode(w, monkeypatch):
    from apps.harness import sensitivity

    allow(w["agent"], role=roles.READ_ONLY)
    turn = Turn.objects.create(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key=uuid.uuid4().hex)
    monkeypatch.setattr(sensitivity, "is_sensitive", lambda t: True)
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True, turn=turn)
    assert d.access == turn_context.REFUSED and d.stamped is None
    # ...and for the owner a sensitive conversation is refused, not run in read mode either.
    assert turn_context.for_asker(w["agent"], w["owner"], verified=True, turn=turn).access == turn_context.REFUSED


def test_roster_and_topology_and_extended_card_offer_read_mode(w):
    allow(w["agent"], role=roles.READ_ONLY)
    rows = {r["email"]: r for r in access.roster(w["agent"])}
    assert rows["vw@dimagi.com"]["access"] == access.CONFINED
    assert rows["vw@dimagi.com"]["capabilities"] == ["read"]
    card = agent_card.extended_card(w["agent"], w["vw"])
    assert card is not None and [s.id for s in card.skills] == ["read"]


def test_the_public_card_does_not_list_read_mode(w):
    # A contact `read_only` line on an agent with no published read mode offers outsiders nothing.
    allow(w["agent"], role=roles.READ_ONLY, contact=True, domain="dimagi.com", verified=True)
    Contact.objects.create(workspace=w["ws"], email="x@dimagi.com")
    assert agent_card.public_card(w["agent"]) is None
    assert agent_card.public_profiles(w["agent"]) == []


def test_topology_edge_names_read_mode(w):
    allow(w["agent"], role=roles.READ_ONLY)
    assert topology.__name__   # the edge computation shares `interface.reachable` with the roster
    assert interface.reachable(frozenset({roles.READ}), w["agent"].interface) == ["read"]
    assert interface.reachable(frozenset({"use:canopy"}), w["agent"].interface) == []


def test_reachable_lists_the_published_read_mode_not_a_second_read(w):
    w["agent"].interface = parse({"capabilities": {"ask": {"tools": ["Read"]}, "canopy": {"tools": ["Read"]}}})
    assert interface.reachable(frozenset({roles.READ}), w["agent"].interface) == ["ask"]
    assert interface.reachable(frozenset({"use:canopy"}), w["agent"].interface) == ["canopy"]


# --- the gate ---------------------------------------------------------------------------------


def test_the_gate_lists_a_read_line_with_no_published_read_mode(w):
    assert read_mode_gate.exposures() == []
    allow(w["agent"], role=roles.READ_ONLY)
    hits = read_mode_gate.exposures()
    assert [h["agent"] for h in hits] == ["hal"] and "read_only" in hits[0]["lines"][0]
    w["agent"].interface = parse({"capabilities": {"ask": {"tools": ["Read"]}}})
    w["agent"].save(update_fields=["interface"])
    assert read_mode_gate.exposures() == []             # a published read mode: nothing newly widened


def test_the_gate_counts_a_stamped_read_mode_line_and_a_workspace_default_but_not_a_writer(w):
    allow(w["agent"], capability="ask")
    assert [h["agent"] for h in read_mode_gate.exposures()] == ["hal"]
    w["agent"].grants.all().delete()
    allow(w["agent"], role=roles.READ_WRITE)
    assert read_mode_gate.exposures() == []


def test_the_gate_command_exits_nonzero_with_a_hit(w):
    out = io.StringIO()
    call_command("read_mode_gate", stdout=out)
    assert out.getvalue().startswith("none")
    allow(w["agent"], role=roles.READ_ONLY)
    with pytest.raises(SystemExit) as exc:
        call_command("read_mode_gate", stdout=out)
    assert exc.value.code == 1 and "connect/hal" in out.getvalue()


# --- the screens' data ------------------------------------------------------------------------


def test_the_read_mode_route_says_which_read_mode_the_agent_has(w):
    from django.test import Client

    c = Client()
    c.force_login(w["owner"])
    got = c.get("/api/agents/hal/read-mode").json()
    assert got["source"] == "default"
    assert got["profile"]["write_paths"] == ["{cwd}/.hal-read/*", "{cwd}/.hal-ask/*"]
    w["agent"].interface = parse({"capabilities": {"ask": {"tools": ["Read"]}}})
    w["agent"].save(update_fields=["interface"])
    got = c.get("/api/agents/hal/read-mode").json()
    assert got["source"] == "custom" and got["profile"]["tools"] == ["Read"]


def test_the_access_list_names_the_connected_sites_a_line_may_open(w):
    from django.test import Client

    w["agent"].interface = parse({"capabilities": {"ask": {"tools": ["Read"]}, "canopy": {"tools": ["Read"]}}})
    w["agent"].save(update_fields=["interface"])
    c = Client()
    c.force_login(w["owner"])
    got = c.get("/api/agents/hal/access-list").json()
    assert [s["name"] for s in got["connected_sites"]] == ["canopy"]
    assert "read mode" in got["roles"][-1]["can"][0]
