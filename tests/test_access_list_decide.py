"""`turn_context.resolve_turn_context` gives everyone but the owner, the admins and canopy exactly what their line says.

An agent's access list
is the only way in beyond its owner and admins, every decision says which role it put the asker in
and why, and a line can only use a profile the agent actually has.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.agents import access, roles, turn_context
from apps.agents.published_profiles import parse
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.contacts.models import Contact
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant

#: `ask` is read mode, what `read` opens; `canopy` is a host site only a line that names it (or
#: `write`) opens.
IFACE = {"capabilities": {
    "ask": {"tools": ["Read"]},
    "canopy": {"tools": ["Read", "Grep"]},
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
    """A line giving `role`, or (role "") the one capability in `capability=`."""
    where = {"agent": w["agent"]} if scope == "agent" else {"workspace": w[scope]}
    return G.objects.create(**where, role=role, **kw)


def _contact(w, email):
    return Contact.objects.create(workspace=w["connect"], email=email)


def _slack_turn(w, contact, assurance="slack_member"):
    return Turn.objects.create(agent=w["agent"], origin=Turn.ORIGIN_SLACK, idempotency_key=uuid.uuid4().hex,
                               initiator_kind="contact", initiator_contact=contact,
                               initiator_assurance=assurance)


# --- every decision now names its role and where it came from ---------------------------

def test_owner_and_admin_are_named_on_the_ladder_and_a_workspace_editor_with_no_line_is_refused(w):
    agent = w["agent"]
    d = turn_context.for_asker(agent, w["owner"], verified=True)
    assert (d.standing.agent_role, d.standing.source) == (roles.OWNER, "owner")
    AgentAdmin.objects.create(agent=agent, user=w["vw"], granted_by=w["owner"])
    d = turn_context.for_asker(agent, w["vw"], verified=True)
    assert (d.standing.agent_role, d.standing.source) == (roles.ADMIN, "admin")
    editor = get_user_model().objects.create_user("ce", "ce@elsewhere.org", "pw")
    M.objects.create(user=editor, workspace=w["connect"], role=M.EDITOR)
    d = turn_context.for_asker(agent, editor, verified=True)
    # Being a workspace editor is not a role on the agent: nothing names them, so nothing.
    assert (d.access, d.standing.agent_role) == (turn_context.REFUSED, None)
    assert "access list" in d.refusal.why and w["owner"].email in d.refusal.how_to_fix


def test_a_line_that_says_manual_only_is_the_old_editor_tier(w):
    editor = get_user_model().objects.create_user("ce", "ce@elsewhere.org", "pw")
    M.objects.create(user=editor, workspace=w["connect"], role=M.EDITOR)
    _line(w, roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.EDITOR, manual_only=True)
    d = turn_context.for_asker(w["agent"], editor, verified=True)
    assert (d.access, d.standing.agent_role, d.review.manual_only) == (access.FULL, roles.EDITOR, True)
    assert d.standing.source.startswith("list:workspace:connect:editor")
    assert d.standing.via == "Everyone in connect, editor or above"


def test_a_confined_decision_is_a_read_only_role_or_a_named_host_site(w):
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True, requested="ask")
    assert (d.access, d.standing.agent_role) == (turn_context.REFUSED, None)        # nothing on the list names them
    # A connect viewer with a line naming ONE host site: not a role.
    _line(w, "", kind=G.PERSON, user=w["vw"], capability="canopy")
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True, requested="canopy")
    assert (d.access, d.stamped, d.standing.agent_role) == (access.CONFINED, "canopy", None)
    assert d.standing.permissions == {roles.use("canopy")} and d.standing.source.startswith("list:person:")
    assert turn_context.for_asker(w["agent"], w["vw"], verified=True, requested="ask").access == turn_context.REFUSED


# --- the list is a way in --------------------------------------------------------------

def test_a_colleague_by_address_is_read_only_through_a_domain_line_with_slack_proof(w):
    """Ismaïla: an editor of `dimagi`, not a member of `connect`, writing in from Slack."""
    contact = _contact(w, "idiene@dimagi.com")
    turn = _slack_turn(w, contact)
    assert turn_context.for_turn(turn, w["agent"]).access == turn_context.REFUSED        # nothing names him yet
    _line(w, roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    d = turn_context.for_turn(turn, w["agent"])
    assert (d.access, d.stamped, d.standing.agent_role) == (access.CONFINED, "ask", roles.READ_ONLY)
    assert d.standing.source.startswith("list:domain:dimagi.com")


def test_an_address_that_is_not_proven_does_not_match_a_verified_domain_line(w):
    _line(w, roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    guest = _slack_turn(w, _contact(w, "x@dimagi.com"), assurance="slack")        # a Slack guest: tier 2
    assert turn_context.for_turn(guest, w["agent"]).access == turn_context.REFUSED


def test_read_only_opens_read_mode_and_a_second_line_opens_a_named_host_site(w):
    _line(w, roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    contact = _contact(w, "idiene@dimagi.com")
    assert turn_context.for_asker(w["agent"], contact, verified=True, requested="ask").access == access.CONFINED
    d = turn_context.for_asker(w["agent"], contact, verified=True, requested="canopy")
    assert d.access == turn_context.REFUSED                                              # read opens read mode and nothing else
    _line(w, "", kind=G.CONTACT, domain="dimagi.com", capability="canopy")
    d = turn_context.for_asker(w["agent"], contact, verified=True, requested="canopy")
    assert (d.access, d.stamped, d.standing.agent_role) == (access.CONFINED, "canopy", roles.READ_ONLY)
    assert d.standing.permissions == {roles.READ, roles.use("canopy")}


def test_write_drives_the_whole_agent_whatever_the_role_is_called(w):
    _line(w, roles.READ_WRITE, kind=G.PERSON, user=w["vw"])
    d = turn_context.for_asker(w["agent"], w["vw"], verified=True)
    assert (d.access, d.standing.agent_role, d.review.manual_only, d.review.may_request_auto) == (
        access.FULL, roles.READ_WRITE, False, False)
    assert d.standing.permissions >= {roles.READ, roles.WRITE} and roles.AGENT_EDIT not in d.standing.permissions


def test_a_line_cannot_use_a_host_site_the_agent_does_not_have(w):
    bare = Agent.objects.create(slug="bare", name="Bare", workspace=w["connect"], owner=w["owner"])
    _line({**w, "agent": bare}, "", kind=G.DOMAIN, domain="dimagi.com", capability="canopy")
    contact = _contact(w, "idiene@dimagi.com")
    assert turn_context.for_asker(bare, contact, verified=True).access == turn_context.REFUSED


def test_read_mode_needs_no_published_profile(w):
    bare = Agent.objects.create(slug="bare", name="Bare", workspace=w["connect"], owner=w["owner"])
    _line({**w, "agent": bare}, roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    contact = _contact(w, "idiene@dimagi.com")
    d = turn_context.for_asker(bare, contact, verified=True)
    assert (d.access, d.stamped) == (access.CONFINED, "read")


def test_everyone_in_another_workspace_can_be_named_and_an_editor_line_runs_the_whole_agent(w):
    """An editor of the root workspace is a stranger in `connect` until the list names them."""
    assert turn_context.for_asker(w["agent"], w["ed"], verified=True).access == turn_context.REFUSED
    _line(w, roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    d = turn_context.for_asker(w["agent"], w["ed"], verified=True)
    assert (d.access, d.standing.agent_role, d.review.manual_only) == (access.FULL, roles.EDITOR, False)
    assert turn_context.for_asker(w["agent"], w["vw"], verified=True).access != access.FULL


def test_a_line_can_keep_the_manual_gate_and_a_second_line_can_lift_it(w):
    gated = _line(w, roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR,
                  manual_only=True)
    assert turn_context.for_asker(w["agent"], w["ed"], verified=True).review.manual_only is True
    _line(w, roles.EDITOR, kind=G.PERSON, user=w["ed"])
    assert turn_context.for_asker(w["agent"], w["ed"], verified=True).review.manual_only is False
    assert gated.pk                                                              # the gated line is still there


def test_the_strongest_line_wins_even_when_a_weaker_one_also_names_them(w):
    editor = get_user_model().objects.create_user("ce", "ce@elsewhere.org", "pw")
    M.objects.create(user=editor, workspace=w["connect"], role=M.EDITOR)
    _line(w, roles.EDITOR, kind=G.PERSON, user=editor)
    _line(w, roles.READ_ONLY, kind=G.WORKSPACE, subject_workspace=w["connect"])
    d = turn_context.for_asker(w["agent"], editor, verified=True)
    assert (d.access, d.standing.agent_role) == (access.FULL, roles.EDITOR)


def test_a_workspace_default_reaches_the_agent_unless_it_opts_out(w):
    G.objects.create(workspace=w["root"], kind=G.DOMAIN, domain="dimagi.com", role=roles.READ_ONLY)
    contact = _contact(w, "idiene@dimagi.com")
    assert turn_context.for_asker(w["agent"], contact, verified=True).access == access.CONFINED
    Agent.objects.filter(pk=w["agent"].pk).update(inherit_workspace_defaults=False)
    w["agent"].refresh_from_db()
    assert turn_context.for_asker(w["agent"], contact, verified=True).access == turn_context.REFUSED


def test_the_sensitive_data_rule_still_confines_a_listed_editor(w):
    iface = {**IFACE, "capabilities": {**IFACE["capabilities"],
             "data": {"tools": ["Read"], "data": "sensitive",
                     "sites": ["connect-labs"]}}}
    Agent.objects.filter(pk=w["agent"].pk).update(interface=parse(iface))
    w["agent"].refresh_from_db()
    _line(w, roles.EDITOR, kind=G.PERSON, user=w["ed"])
    d = turn_context.for_asker(w["agent"], w["ed"], verified=True, requested="data")
    assert (d.access, d.stamped, d.standing.agent_role, d.basis) == (
        access.CONFINED, "data", roles.EDITOR, "sensitive:data")


def test_the_roster_shows_what_the_list_gives(w):
    _line(w, roles.READ_ONLY, kind=G.PERSON, user=w["vw"])
    _line(w, "", kind=G.PERSON, user=w["vw"], capability="canopy")
    row = next(r for r in access.roster(w["agent"]) if r["user_id"] == w["vw"].pk)
    assert row["access"] == access.CONFINED and row["profiles"] == ["ask", "canopy"]
    assert (row["role"], row["source"]) == (roles.READ_ONLY, f"list:person:{w['vw'].pk}")
    assert row["via"] == "vw@dimagi.com"
    rows = {r["user_id"]: r for r in access.roster(w["agent"])}
    assert (rows[w["owner"].pk]["role"], rows[w["owner"].pk]["source"]) == (roles.OWNER, roles.OWNER)


def test_the_envelope_tells_an_agent_the_role_and_why(w):
    from apps.harness import caller_context

    turn = _slack_turn(w, _contact(w, "idiene@dimagi.com"))
    env = caller_context.build(turn)
    assert (env["role"], env["source"]) == (None, "")                          # refused: nothing names him
    _line(w, roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    env = caller_context.build(turn)
    assert env["role"] == roles.READ_ONLY and env["source"].startswith("list:domain:dimagi.com")
    assert env["relationship"] == "contact"                                    # the older word is still sent
