"""GET /api/agents/{slug}/access — everyone's role on an agent, in one place.

The roster composes four sources (owner, explicit admins, implicit workspace-owner
admins, the published interface). It must say exactly what the harness would
do, so the parity test below checks it against `interface.capability_for` for a
real signed-in turn of every member.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import interface
from apps.agents.models import Agent, AgentAdmin
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture()
def world():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")     # workspace owner
    op = User.objects.create_user("op", "op@dimagi.com", "pw")           # agent owner
    adm = User.objects.create_user("adm", "adm@dimagi.com", "pw")        # granted admin
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")           # plain editor
    vw = User.objects.create_user("vw", "vw@partner.org", "pw")          # viewer, other domain
    out = User.objects.create_user("out", "out@else.org", "pw")          # not a member
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    for u, role in ((boss, M.OWNER), (op, M.EDITOR), (adm, M.EDITOR), (ed, M.EDITOR), (vw, M.VIEWER)):
        M.objects.create(user=u, workspace=ws, role=role)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    AgentAdmin.objects.create(agent=agent, user=adm, granted_by=op)
    return {"boss": boss, "op": op, "adm": adm, "ed": ed, "vw": vw, "out": out, "agent": agent}


def _get(user, slug="ace"):
    c = Client()
    c.force_login(user)
    return c.get(f"/api/agents/{slug}/access")


def _by_email(body):
    return {r["email"]: r for r in body["members"]}


def test_every_member_listed_with_role_and_reason(world):
    r = _get(world["ed"])
    assert r.status_code == 200
    rows = _by_email(r.json())
    assert set(rows) == {"boss@dimagi.com", "op@dimagi.com", "adm@dimagi.com", "ed@dimagi.com", "vw@partner.org"}
    assert (rows["op@dimagi.com"]["agent_role"], rows["op@dimagi.com"]["basis"]) == ("owner", "Owns this agent")
    assert (rows["boss@dimagi.com"]["agent_role"], rows["boss@dimagi.com"]["basis"]) == ("admin", "Owns the workspace")
    assert rows["adm@dimagi.com"]["agent_role"] == "admin"
    assert rows["adm@dimagi.com"]["basis"] == "Made admin by op@dimagi.com"
    assert rows["ed@dimagi.com"]["agent_role"] == "member"
    # strongest role first
    assert [m["agent_role"] for m in r.json()["members"]][:3] == ["owner", "admin", "admin"]


def test_no_interface_means_everyone_has_full_access(world):
    body = _get(world["ed"]).json()
    assert body["interface_published"] is False
    assert {r["access"] for r in body["members"]} == {"full"}
    assert body["outsiders"] == []


def test_published_interface_confines_members_and_names_outsiders(world):
    a = world["agent"]
    a.interface = interface.parse({
        "full": ["member@dimagi.com:verified", "contact@dimagi.com:verified"],
        "capabilities": {
            "ask": {"description": "Ask a question", "callers": ["member", "contact"]},
            "status": {"description": "Status", "callers": ["member@dimagi.com"]},
        },
    })
    a.save()
    body = _get(world["ed"]).json()
    rows = _by_email(body)
    assert body["interface_published"] is True
    # a dimagi member is lifted to full by the full: rule
    assert rows["ed@dimagi.com"]["access"] == "full"
    assert rows["ed@dimagi.com"]["full_rule"] == "member@dimagi.com:verified"
    # a partner-domain viewer is confined to what lists plain `member`
    assert rows["vw@partner.org"]["access"] == "confined"
    assert rows["vw@partner.org"]["capabilities"] == ["ask"]
    # admins are never confined
    assert rows["adm@dimagi.com"]["access"] == "full"
    assert {(o["caller"], o["access"], o["capability"]) for o in body["outsiders"]} == {
        ("contact@dimagi.com:verified", "full", None),
        ("contact", "confined", "ask"),
    }


def test_member_listed_nowhere_has_no_access(world):
    a = world["agent"]
    a.interface = interface.parse({
        "capabilities": {"ask": {"description": "Ask", "callers": ["member@dimagi.com"]}},
    })
    a.save()
    rows = _by_email(_get(world["boss"]).json())
    assert rows["vw@partner.org"]["access"] == "none"
    assert rows["vw@partner.org"]["capabilities"] == []


def test_a_grant_whose_holder_left_is_not_listed(world):
    M.objects.filter(user=world["adm"]).delete()
    assert "adm@dimagi.com" not in _by_email(_get(world["ed"]).json())


def test_non_member_gets_404(world):
    assert _get(world["out"]).status_code == 404


@pytest.mark.parametrize("published", [False, True])
def test_roster_agrees_with_the_harness_for_a_signed_in_turn(world, published):
    a = world["agent"]
    if published:
        a.interface = interface.parse({
            "full": ["member@dimagi.com:verified"],
            "capabilities": {"ask": {"description": "Ask", "callers": ["member@partner.org"]}},
        })
        a.save()
    rows = _by_email(_get(world["boss"]).json())
    for key in ("boss", "op", "adm", "ed", "vw"):
        user = world[key]
        turn = Turn(agent=a, prompt="hi", initiator_kind=who.USER,
                    initiator_assurance=who.SESSION, initiator_user=user)
        decided = interface.capability_for(turn, a)
        row = rows[user.email]
        if decided == interface.FULL:
            assert row["access"] == "full", user.email
        elif decided is None:
            assert row["access"] == "none", user.email
        else:
            assert row["access"] == "confined" and decided in row["capabilities"], user.email
