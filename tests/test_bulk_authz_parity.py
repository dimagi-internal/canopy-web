"""The bulk reads must answer exactly what the one-at-a-time reads answer.

A view that judges many (person, workspace) or (person, agent) pairs reads every
fact once — `wsvc.member_roles_of` for roles, `access.Standings` for an agent's
grants — and asks the rule with what it read, instead of a membership read per
pair (which made the agent topology N² queries). That is a second way to reach
the same facts, so this file holds it to the first: every role
`member_roles_of` reports is `member_role`'s, and every `decide` from a
`Standings` is `decide` reading for itself, over a tree with inherited owners
and admins, weaker direct rows, a separate tree, a capped caller, explicit
grants and each kind of interface. The same holds for the tree walks
(`ancestor_chains`, `descendant_map`) and the runner reads
(`routing.runner_tenant_slugs_of`, `administered_runner_ids`), each checked here
against the rule written out longhand.
"""
from __future__ import annotations

import itertools

import pytest
from django.contrib.auth import get_user_model

from apps.agents import access, interface
from apps.agents.models import Agent, AgentAdmin
from apps.harness import routing
from apps.harness.models import Runner, RunnerAdmin
from apps.harness.runners import administered_runner_ids
from apps.workspaces import permissions as perms
from apps.workspaces import role_cap
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership as M, ancestor_chains
from apps.workspaces.testing import enrolled

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


def test_admin_lists_are_is_admin(world):
    users, _, agents = world
    standings = access.Standings(users, agents)
    for a in agents:
        # A listing of people, so judged as themselves: freshly read, no acting cap.
        assert a.admin_user_ids() == {u.pk for u in users if a.is_admin(User.objects.get(pk=u.pk))}, a.slug
        for u in users:
            assert standings.is_admin(u, a) == a.is_admin(u), (u.email, a.slug)


def _walk_up(slug):
    out, pid = [], Workspace.objects.get(slug=slug).parent_id
    while pid:
        out.append(pid)
        pid = Workspace.objects.get(slug=pid).parent_id
    return out


def test_tree_walks_are_the_walk_longhand(world):
    _, workspaces, _ = world
    chains = ancestor_chains({w.slug: w.parent_id for w in workspaces})
    below = wsvc.descendant_map({w.slug for w in workspaces})
    for w in workspaces:
        assert chains[w.slug] == _walk_up(w.slug) == w.ancestor_slugs()
        assert below[w.slug] == {x.slug for x in workspaces if w.slug in _walk_up(x.slug)}
        assert wsvc.descendant_slugs({w.slug}) == below[w.slug]


def test_runner_reads_are_the_rule_longhand(world):
    users, workspaces, _ = world
    by = {u.email.split("@")[0]: u for u in users}
    root, div, sub, other = workspaces
    boxes = [
        enrolled(Runner.objects.create(name="org", kind=Runner.EMDASH, workspace=root, owner=by["jj"]), root),
        enrolled(Runner.objects.create(name="div", kind=Runner.EMDASH, workspace=div, owner=by["divown"]), div, other),
        enrolled(Runner.objects.create(name="ed", kind=Runner.EMDASH, workspace=sub, owner=by["ed"]), root),
        Runner.objects.create(name="none", kind=Runner.EMDASH, workspace=root, owner=by["vw"]),
        Runner.objects.create(name="orphan", kind=Runner.EMDASH, workspace=root, owner=None),
    ]
    RunnerAdmin.objects.create(runner=boxes[1], user=by["ed"])
    tenants = routing.runner_tenant_slugs_of(boxes)
    for r in boxes:
        longhand = (perms.slugs_with(r.owner, perms.AGENT_WORK, within=routing.covered_slugs(r))
                    if r.owner_id and routing.covered_slugs(r) else set())
        assert tenants[r.pk] == longhand == routing.runner_tenant_slugs(r), r.name
    for u in users:
        mine = administered_runner_ids(u, boxes)
        assert mine == {r.pk for r in boxes if r.owner_id and (
            r.owner_id == u.pk or RunnerAdmin.objects.filter(runner=r, user=u).exists())}, u.email
