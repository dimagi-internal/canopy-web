"""`access.decide` reads the access list alongside an agent's published rules.

`docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`, step 2: whatever the
list gives the asker is merged with what the interface already gave them and the more
permissive answer stands, so an agent with an empty list decides exactly as it always did
(`tests/test_access_matrix.py` is the oracle for that) and every decision now says which role
it put the asker in and why.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.agents import access, roles
from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.contacts.models import Contact
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant

#: A viewer door (`ask`, open to contacts) and a user door (`canopy`, members only).
IFACE = {"capabilities": {
    "ask": {"callers": [], "role": "viewer", "tools": ["Read"]},
    "canopy": {"callers": [], "role": "user", "tools": ["Read", "Grep"]},
}}


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    boss, op, ed, vw, out = (mk(n, f"{n}@dimagi.com", "pw") for n in ("boss", "op", "ed", "vw", "out"))
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=boss)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss, parent=root)
    M.objects.create(user=boss, workspace=root, role=M.OWNER)
    M.objects.create(user=op, workspace=connect, role=M.EDITOR)
    M.objects.create(user=ed, workspace=root, role=M.EDITOR)         # an editor of dimagi, NOT of connect
    M.objects.create(user=vw, workspace=connect, role=M.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=op,
                                 interface=parse(IFACE))
    return {"root": root, "connect": connect, "agent": agent, "owner": op, "ed": ed, "vw": vw,
            "out": out, "boss": boss}


def _line(w, role, scope="agent", **kw):
    where = {"agent": w["agent"]} if scope == "agent" else {"workspace": w[scope]}
    return G.objects.create(**where, role=role, **kw)


def _contact(w, email):
    return Contact.objects.create(workspace=w["connect"], email=email)


def _slack_turn(w, contact, assurance="slack_member"):
    return Turn.objects.create(agent=w["agent"], origin=Turn.ORIGIN_SLACK, idempotency_key=uuid.uuid4().hex,
                               initiator_kind="contact", initiator_contact=contact,
                               initiator_assurance=assurance)


# --- every decision now names its role and where it came from ---------------------------

def test_the_rules_an_agent_was_published_with_are_named_on_the_ladder(w):
    agent = w["agent"]
    Agent.objects.filter(pk=agent.pk).update(interface=parse({**IFACE, "full": ["member@dimagi.com"]}))
    agent.refresh_from_db()
    d = access.decide(agent, w["owner"], verified=True)
    assert (d.agent_role, d.source) == (roles.OWNER, "owner")
    AgentAdmin.objects.create(agent=agent, user=w["vw"], granted_by=w["owner"])
    d = access.decide(agent, w["vw"], verified=True)
    assert (d.agent_role, d.source) == (roles.ADMIN, "admin")
    M.objects.create(user=w["out"], workspace=w["connect"], role=M.EDITOR)
    d = access.decide(agent, w["out"], verified=True)
    # `out` is a connect editor AND a member at dimagi.com: the `full:` rule is the more
    # permissive of the two (no manual gate), so it is what they are named by.
    assert (d.agent_role, d.source, d.manual_only) == (roles.EDITOR, "rule:member@dimagi.com", False)


def test_the_implicit_workspace_editor_tier_is_an_editor_whose_turns_are_manual(w):
    d = access.decide(w["agent"], w["owner"], verified=True)    # owner: baseline
    assert d.agent_role == roles.OWNER
    editor = get_user_model().objects.create_user("ce", "ce@elsewhere.org", "pw")
    M.objects.create(user=editor, workspace=w["connect"], role=M.EDITOR)
    d = access.decide(w["agent"], editor, verified=True)
    assert (d.access, d.agent_role, d.source, d.manual_only) == (
        access.FULL, roles.EDITOR, "workspace-editor", True)


def test_a_confined_decision_is_a_viewer_or_a_user_by_the_door_it_opens(w):
    d = access.decide(w["agent"], w["vw"], verified=True, capability="ask")
    assert (d.access, d.agent_role) == (access.NONE, None)        # the doors name no callers, the list no one
    # A connect viewer with an explicit list line: the door decides viewer vs user.
    _line(w, roles.USER, kind=G.PERSON, user=w["vw"])
    d = access.decide(w["agent"], w["vw"], verified=True, capability="canopy")
    assert (d.access, d.capability, d.agent_role) == (access.CONFINED, "canopy", roles.USER)


# --- the list is a way in --------------------------------------------------------------

def test_a_colleague_by_address_is_a_viewer_through_a_domain_line_with_slack_proof(w):
    """Ismaïla: an editor of `dimagi`, not a member of `connect`, writing in from Slack."""
    contact = _contact(w, "idiene@dimagi.com")
    turn = _slack_turn(w, contact)
    assert access.decide_for_turn(turn, w["agent"]).access == access.NONE        # nothing names him yet
    _line(w, roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    d = access.decide_for_turn(turn, w["agent"])
    assert (d.access, d.capability, d.agent_role) == (access.CONFINED, "ask", roles.VIEWER)
    assert d.source.startswith("list:domain:dimagi.com")


def test_an_address_that_is_not_proven_does_not_match_a_verified_domain_line(w):
    _line(w, roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    guest = _slack_turn(w, _contact(w, "x@dimagi.com"), assurance="slack")        # a Slack guest: tier 2
    assert access.decide_for_turn(guest, w["agent"]).access == access.NONE


def test_a_viewer_opens_viewer_doors_only_and_a_user_opens_both(w):
    _line(w, roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    contact = _contact(w, "idiene@dimagi.com")
    d = access.decide(w["agent"], contact, verified=True, capability="canopy")
    assert d.access == access.NONE                                              # a viewer cannot open a user door
    G.objects.filter(agent=w["agent"]).update(role=roles.USER)
    d = access.decide(w["agent"], contact, verified=True, capability="canopy")
    assert (d.access, d.capability, d.agent_role) == (access.CONFINED, "canopy", roles.USER)


def test_a_line_cannot_open_a_door_the_agent_does_not_have(w):
    bare = Agent.objects.create(slug="bare", name="Bare", workspace=w["connect"], owner=w["owner"])
    _line({**w, "agent": bare}, roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    contact = _contact(w, "idiene@dimagi.com")
    assert access.decide(bare, contact, verified=True).access == access.NONE


def test_everyone_in_another_workspace_can_be_named_and_an_editor_line_runs_the_whole_agent(w):
    """An editor of the root workspace is a stranger in `connect` until the list names them."""
    assert access.decide(w["agent"], w["ed"], verified=True).access == access.NONE
    _line(w, roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    d = access.decide(w["agent"], w["ed"], verified=True)
    assert (d.access, d.agent_role, d.manual_only) == (access.FULL, roles.EDITOR, False)
    assert access.decide(w["agent"], w["vw"], verified=True).access != access.FULL


def test_a_line_can_keep_the_manual_gate_and_a_second_line_can_lift_it(w):
    gated = _line(w, roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR,
                  manual_only=True)
    assert access.decide(w["agent"], w["ed"], verified=True).manual_only is True
    _line(w, roles.EDITOR, kind=G.PERSON, user=w["ed"])
    assert access.decide(w["agent"], w["ed"], verified=True).manual_only is False
    assert gated.pk                                                              # the gated line is still there


def test_the_list_never_lowers_what_the_published_rules_gave(w):
    editor = get_user_model().objects.create_user("ce", "ce@elsewhere.org", "pw")
    M.objects.create(user=editor, workspace=w["connect"], role=M.EDITOR)
    _line(w, roles.VIEWER, kind=G.PERSON, user=editor)
    d = access.decide(w["agent"], editor, verified=True)
    assert (d.access, d.agent_role) == (access.FULL, roles.EDITOR)


def test_a_workspace_default_reaches_the_agent_unless_it_opts_out(w):
    G.objects.create(workspace=w["root"], kind=G.DOMAIN, domain="dimagi.com", role=roles.VIEWER)
    contact = _contact(w, "idiene@dimagi.com")
    assert access.decide(w["agent"], contact, verified=True).access == access.CONFINED
    Agent.objects.filter(pk=w["agent"].pk).update(inherit_workspace_defaults=False)
    w["agent"].refresh_from_db()
    assert access.decide(w["agent"], contact, verified=True).access == access.NONE


def test_the_sensitive_data_rule_still_confines_a_listed_editor(w):
    iface = {**IFACE, "capabilities": {**IFACE["capabilities"],
             "data": {"callers": [], "role": "user", "tools": ["Read"], "data": "sensitive",
                     "sites": ["connect-labs"]}}}
    Agent.objects.filter(pk=w["agent"].pk).update(interface=parse(iface))
    w["agent"].refresh_from_db()
    _line(w, roles.EDITOR, kind=G.PERSON, user=w["ed"])
    d = access.decide(w["agent"], w["ed"], verified=True, capability="data")
    assert (d.access, d.capability, d.agent_role, d.basis) == (
        access.CONFINED, "data", roles.EDITOR, "sensitive:data")


def test_the_roster_shows_what_the_list_gives(w):
    _line(w, roles.USER, kind=G.PERSON, user=w["vw"])
    row = next(r for r in access.roster(w["agent"]) if r["user_id"] == w["vw"].pk)
    assert row["access"] == access.CONFINED and row["capabilities"] == ["ask", "canopy"]
    assert (row["role"], row["source"]) == (roles.USER, f"list:person:{w['vw'].pk}")
    rows = {r["user_id"]: r for r in access.roster(w["agent"])}
    assert (rows[w["owner"].pk]["role"], rows[w["owner"].pk]["source"]) == (roles.OWNER, roles.OWNER)


def test_the_envelope_tells_an_agent_the_role_and_why(w):
    from apps.harness import caller_context

    turn = _slack_turn(w, _contact(w, "idiene@dimagi.com"))
    env = caller_context.build(turn)
    assert (env["role"], env["source"]) == (None, "")                          # refused: nothing names him
    _line(w, roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    env = caller_context.build(turn)
    assert env["role"] == roles.VIEWER and env["source"].startswith("list:domain:dimagi.com")
    assert env["relationship"] == "contact"                                    # the older word is still sent
