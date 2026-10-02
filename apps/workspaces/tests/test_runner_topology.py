"""GET /api/workspaces/{slug}/runner-topology — the subtree's agents and the boxes they run on."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
OWNER, EDITOR = WorkspaceMembership.OWNER, WorkspaceMembership.EDITOR


def _user(email):
    return User.objects.create(username=email, email=email)


def _get(user, slug):
    c = Client()
    c.force_login(user)
    return c.get(f"/api/workspaces/{slug}/runner-topology")


@pytest.fixture
def tree():
    ceo, staff, outsider = _user("ceo@dimagi.com"), _user("staff@dimagi.com"), _user("x@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=ceo)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=ceo, parent=dimagi)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=ceo)
    WorkspaceMembership.objects.create(workspace=dimagi, user=ceo, role=OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=staff, role=EDITOR)
    WorkspaceMembership.objects.create(workspace=other, user=outsider, role=OWNER)

    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, workspace=dimagi, paired_by=ceo)
    # Homed outside the tree, paired by someone with no membership in it: it
    # can be assigned, and can never claim.
    stray = Runner.objects.create(name="stray", kind=Runner.CLOUD, workspace=other, paired_by=outsider)
    Runner.objects.create(name="dead", kind=Runner.EMDASH, workspace=dimagi, status=Runner.RETIRED)
    Runner.objects.create(name="elsewhere", kind=Runner.EMDASH, workspace=other, paired_by=outsider)

    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    Agent.objects.create(slug="eva", name="Eva", workspace=dimagi)
    RunnerAssignment.objects.create(agent=hal, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=hal, runner=stray, rank=1, enabled=False)
    RunnerAssignment.objects.create(agent=hal, runner=stray, rank=0, source="email", strict=True)
    return {"ceo": ceo, "staff": staff, "outsider": outsider}


def test_owner_sees_the_subtree_in_tree_order(tree):
    r = _get(tree["ceo"], "dimagi")
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == "dimagi"
    assert [(w["slug"], w["depth"]) for w in body["workspaces"]] == [("dimagi", 0), ("connect", 1)]
    assert [a["slug"] for a in body["workspaces"][0]["agents"]] == ["eva"]
    assert body["workspaces"][0]["agents"][0]["routes"] == []


def test_routes_carry_rules_and_whether_the_runner_can_claim(tree):
    body = _get(tree["ceo"], "dimagi").json()
    runners = {r["id"]: r for r in body["runners"]}
    hal = body["workspaces"][1]["agents"][0]
    rows = [(runners[x["runner_id"]]["name"], x["source"], x["enabled"], x["can_claim"]) for x in hal["routes"]]
    assert ("laptop", "", True, True) in rows
    assert ("stray", "", False, False) in rows
    assert ("stray", "email", True, False) in rows


def test_runners_are_home_plus_referenced_never_retired_or_unrelated(tree):
    body = _get(tree["ceo"], "dimagi").json()
    by_name = {r["name"]: r for r in body["runners"]}
    assert set(by_name) == {"laptop", "stray"}
    assert by_name["laptop"]["in_tree"] and by_name["laptop"]["agent_count"] == 1
    assert not by_name["stray"]["in_tree"] and by_name["stray"]["workspace"] == "other"
    assert [r["name"] for r in body["runners"]] == ["laptop", "stray"]  # in-tree first


def test_rooted_at_a_division_shows_only_that_branch(tree):
    body = _get(tree["ceo"], "connect").json()
    assert [w["slug"] for w in body["workspaces"]] == ["connect"]
    assert body["workspaces"][0]["depth"] == 0 and body["workspaces"][0]["parent"] is None


def test_editor_is_refused_and_non_member_sees_nothing(tree):
    assert _get(tree["staff"], "dimagi").status_code == 403
    assert _get(tree["outsider"], "dimagi").status_code == 404


def test_admin_sees_only_the_workspaces_they_administer(tree):
    admin = _user("ops@dimagi.com")
    WorkspaceMembership.objects.create(
        workspace=Workspace.objects.get(slug="dimagi"), user=admin, role=WorkspaceMembership.ADMIN,
    )
    body = _get(admin, "dimagi").json()
    # Only ownership flows down the tree: admin of the org is not admin of a division.
    assert [w["slug"] for w in body["workspaces"]] == ["dimagi"]
    WorkspaceMembership.objects.create(
        workspace=Workspace.objects.get(slug="connect"), user=admin, role=WorkspaceMembership.ADMIN,
    )
    assert [w["slug"] for w in _get(admin, "dimagi").json()["workspaces"]] == ["dimagi", "connect"]
