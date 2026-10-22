"""The bulk reads must answer exactly what the one-at-a-time reads answer.

A view that judges many (person, workspace) or (person, agent) pairs reads every
fact once — `wsvc.member_roles_of` for roles, `access.Standings` for an agent's
grants — and asks the rule with what it read, instead of a membership read per
pair (which made the agent topology N² queries). That is a second way to reach
the same facts, so this file holds it to the first: every role
`member_roles_of` reports is `member_role`'s, and every `decide` from a
`Standings` is `decide` reading for itself, over a tree with inherited owners
and admins, weaker direct rows, a separate tree, a capped caller, explicit
grants and each kind of interface.
"""
from __future__ import annotations

import itertools

import pytest
from django.contrib.auth import get_user_model

from apps.agents import access, interface
from apps.agents.models import Agent, AgentAdmin
from apps.workspaces import role_cap
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership as M

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


@pytest.fixture
def world():
    jj = _user("jj@dimagi.com")
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    div = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=root)
    sub = Workspace.objects.create(slug="labs", display_name="Labs", created_by=jj, parent=div)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=jj)
    people = {
        "jj": jj, "adm": _user("adm@dimagi.com"), "divown": _user("divown@dimagi.com"),
        "ed": _user("ed@dimagi.com"), "vw": _user("vw@dimagi.com"), "out": _user("out@else.org"),
        "capped": _user("capped@dimagi.com"),
    }
    rows = [
        (root, "jj", M.OWNER), (root, "adm", M.ADMIN), (sub, "adm", M.VIEWER),  # weaker direct row below
        (div, "divown", M.OWNER), (root, "divown", M.VIEWER), (sub, "ed", M.EDITOR), (root, "vw", M.VIEWER),
        (other, "out", M.OWNER), (root, "capped", M.OWNER),
    ]
    for ws, who, role in rows:
        M.objects.create(workspace=ws, user=people[who], role=role)
    role_cap.apply(people["capped"], role_cap.EDITOR)
    ifaces = [{}, {"capabilities": {"ask": {"callers": ["member"]}}}, {"full": ["member"]}]
    agents = []
    for i, (ws, owner) in enumerate([(root, "jj"), (div, "divown"), (sub, "ed"), (sub, "vw"), (other, "out")]):
        a = Agent.objects.create(slug=f"agent{i}", name=f"A{i}", workspace=ws, owner=people[owner],
                                 interface=interface.parse(ifaces[i % 3]) if ifaces[i % 3] else {})
        agents.append(a)
    AgentAdmin.objects.create(agent=agents[2], user=people["vw"])
    AgentAdmin.objects.create(agent=agents[1], user=people["ed"])
    AgentAdmin.objects.create(agent=agents[0], user=people["out"])  # a grant held by a non-member
    return list(people.values()), [root, div, sub, other], agents


def test_member_roles_of_is_member_role_for_every_person_and_workspace(world):
    users, workspaces, _ = world
    bulk = wsvc.member_roles_of(users)
    for u, ws in itertools.product(users, workspaces):
        assert bulk[u.pk].get(ws.slug) == wsvc.member_role(u, ws.slug), (u.email, ws.slug)
        assert wsvc.member_roles(u) == bulk[u.pk]


def test_decide_from_standings_is_decide_reading_for_itself(world):
    users, _, agents = world
    standings = access.Standings(users, agents)
    for u, a in itertools.product(users, agents):
        assert access.decide(a, u, verified=True, standing=standings.of(u, a)) == access.decide(a, u, verified=True), \
            (u.email, a.slug)


def test_standings_refuses_a_person_it_was_not_given(world):
    users, _, agents = world
    with pytest.raises(KeyError):
        access.Standings(users[:1], agents).of(users[1], agents[0])
