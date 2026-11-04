"""A role a workspace defined resolves exactly as a built-in does: through its row, by permission.

The decision never reads a role's name. These tests define roles of their own and hold the resolver,
the bulk read, the envelope and the safety rails to that.
"""
from __future__ import annotations

import itertools

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.agents import access, grants, roles
from apps.agents.interface import _held_permissions
from apps.agents.models import Agent, AgentGrant
from apps.workspaces.models import Workspace
from apps.workspaces.models import WorkspaceMembership as M
from tests.access_helpers import declare, make_role

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


@pytest.fixture
def w():
    jj = _user("jj@dimagi.com")
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    child = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=root)
    people = {k: _user(f"{k}@dimagi.com") for k in ("owner", "door", "writer", "reader", "tool")}
    for p in people.values():
        M.objects.create(workspace=root, user=p, role=M.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=child, owner=people["owner"])
    declare(agent, {"connect": {}, "report": {}})
    return {"root": root, "child": child, "agent": agent, **people}


def _line(agent, user, role):
    return AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=user, role=role)


def test_a_door_role_opens_that_door_and_nothing_else(w):
    make_role(w["root"], "connector", {roles.READ, roles.use("connect")})
    _line(w["agent"], w["door"], "connector")
    d = access.decide(w["agent"], w["door"], verified=True, capability="connect")
    assert d.access == access.CONFINED and roles.can_use(d.permissions, "connect")
    assert not roles.can_use(d.permissions, "report") and not roles.holds(d.permissions, roles.WRITE)
    assert d.agent_role == "connector"


def test_a_role_holding_write_drives_the_agent_and_carries_read_and_every_door(w):
    make_role(w["root"], "pilot", {roles.WRITE})
    _line(w["agent"], w["writer"], "pilot")
    d = access.decide(w["agent"], w["writer"], verified=True)
    assert d.access == access.FULL and roles.holds(d.permissions, roles.READ)
    assert roles.can_use(d.permissions, "anything")


def test_editing_a_role_changes_its_holders_from_the_next_read(w):
    row = make_role(w["root"], "pilot", {roles.READ})
    _line(w["agent"], w["writer"], "pilot")
    assert access.decide(w["agent"], w["writer"], verified=True).access != access.FULL
    row.permissions = sorted(roles.validate_permissions({roles.WRITE}))
    row.save()
    assert access.decide(w["agent"], w["writer"], verified=True).access == access.FULL


def test_the_envelope_role_is_the_key_and_permissions_are_the_contract(w):
    make_role(w["root"], "connector", {roles.use("connect")})
    _line(w["agent"], w["door"], "connector")
    d = access.decide(w["agent"], w["door"], verified=True, capability="connect")
    assert d.agent_role == "connector"
    assert "use:connect" in _held_permissions(d, "connect")


def test_the_strongest_role_of_a_person_is_the_one_that_allows_most(w):
    make_role(w["root"], "connector", {roles.READ, roles.use("connect")})
    make_role(w["root"], "pilot", {roles.WRITE})
    _line(w["agent"], w["door"], "connector")
    AgentGrant.objects.create(workspace=w["root"], kind=AgentGrant.PERSON, user=w["door"], role="pilot")
    got = grants.resolve(w["agent"], grants.Asker(user=w["door"], verified=True))
    assert got.role == "pilot" and roles.holds(got.permissions, roles.WRITE)


def test_a_role_defined_on_the_child_is_not_visible_to_the_parent(w):
    make_role(w["child"], "local", {roles.READ, roles.use("report")})
    assert grants.check_role(AgentGrant.PERSON, "local", "connect").key == "local"
    with pytest.raises(grants.LineError):
        grants.check_role(AgentGrant.PERSON, "local", "dimagi")


def test_an_address_only_line_can_never_give_a_custom_role_that_drives(w):
    make_role(w["root"], "pilot", {roles.WRITE})
    make_role(w["root"], "editor2", {roles.AGENT_EDIT})
    make_role(w["root"], "connector", {roles.READ, roles.use("connect")})
    for kind, key in itertools.product((AgentGrant.DOMAIN, AgentGrant.CONTACT), ("pilot", "editor2")):
        with pytest.raises(grants.LineError, match="drive the whole agent"):
            grants.check_role(kind, key, "connect")
    assert grants.check_role(AgentGrant.DOMAIN, "connector", "connect").key == "connector"


def test_standings_agree_with_decide_for_custom_roles(w):
    make_role(w["root"], "connector", {roles.READ, roles.use("connect")})
    make_role(w["root"], "pilot", {roles.WRITE})
    _line(w["agent"], w["door"], "connector")
    _line(w["agent"], w["writer"], "pilot")
    users = [w[k] for k in ("owner", "door", "writer", "reader", "tool")]
    standings = access.Standings(users, [w["agent"]])
    for u in users:
        for cap in (None, "connect", "report"):
            assert (access.decide(w["agent"], u, verified=True, capability=cap, standing=standings.of(u, w["agent"]))
                    == access.decide(w["agent"], u, verified=True, capability=cap)), (u.email, cap)


def test_reading_lines_costs_the_same_with_one_custom_role_or_twenty(w):
    def cost():
        with CaptureQueriesContext(connection) as q:
            grants.lines_for_many([w["agent"]])
            access.Standings([w["door"]], [w["agent"]])
        return len(q)

    make_role(w["root"], "r0", {roles.READ, roles.use("connect")})
    _line(w["agent"], w["door"], "r0")
    one = cost()
    for i in range(1, 20):
        make_role(w["root"], f"r{i}", {roles.READ, roles.use("report")})
        AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.DOMAIN, domain=f"d{i}.org", role=f"r{i}")
    assert cost() == one
    lines = grants.lines_for(w["agent"])
    with CaptureQueriesContext(connection) as q:
        for ln in lines:
            _ = ln.permissions
    assert len(q) == 0     # the row rode with the line


def test_validation_refuses_admin_own_empty_and_unknown():
    for bad in ({roles.ADMINISTER}, {roles.OWN}, set(), {"use:*"}, {"wirte"}, {"use:Bad Door"}):
        with pytest.raises(roles.InvalidRole):
            roles.validate_permissions(bad)
    assert roles.validate_permissions({roles.AGENT_EDIT}) >= {roles.WRITE, roles.READ}
    assert roles.validate_permissions({roles.use("connect")}) == {"use:connect"}
