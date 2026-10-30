"""A workspace editor who is only a user: the access list gives editors a `user` line, so they go
through the capabilities like any member instead of driving the whole agent. The retired
`editors: confined` setting is refused at publish and points at the access list.
docs/architecture/access.md; the door-by-door matrix is in tests/test_access_matrix.py."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import access, roles
from apps.agents.interface import InterfaceError, parse, validate
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.workspaces.models import Workspace, WorkspaceMembership as M

from tests.access_helpers import editors_run, publish

ASK = {"ask": {"callers": ["member"], "tools": ["Read"]}}
SUPPORT = {"editors": "confined", "capabilities": ASK}


@pytest.mark.parametrize("value", ["confined", "full", "confine", "", None, True])
def test_the_editors_setting_is_retired_and_points_at_the_access_list(value):
    with pytest.raises(InterfaceError) as exc:
        parse({"editors": value, "capabilities": {"ask": {"tools": ["Read"]}}})
    assert "access list" in str(exc.value)
    assert validate({"editors": value, "capabilities": {"ask": {"tools": ["Read"]}}})


@pytest.fixture()
def w(db):
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    adm = User.objects.create_user("adm", "adm@dimagi.com", "pw")
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    vw = User.objects.create_user("vw", "vw@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=op)
    for u, role in ((op, M.OWNER), (adm, M.EDITOR), (ed, M.EDITOR), (vw, M.VIEWER)):
        M.objects.create(user=u, workspace=ws, role=role)
    agent = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=op,
                                 turn_mode="auto")
    AgentAdmin.objects.create(agent=agent, user=adm, granted_by=op)
    return {"agent": agent, "owner": op, "admin": adm, "editor": ed, "viewer": vw}


def _editors_are_users(agent, role=roles.USER):
    """Everyone in the workspace at least editor gets only a `user` line."""
    return AgentGrant.objects.create(
        agent=agent, kind=AgentGrant.WORKSPACE, subject_workspace_id=agent.workspace_id,
        at_least="editor", role=role)


def _support(w):
    publish(w["agent"], {"capabilities": ASK}, editors=False)
    _editors_are_users(w["agent"])


def test_editor_who_is_only_a_user_is_confined_to_ask_with_no_manual_cap(w):
    _support(w)
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.capability, d.basis) == (access.CONFINED, "ask", "capability:ask")
    assert d.manual_only is False and d.may_request_auto is False
    row = next(r for r in access.roster(w["agent"]) if r["user_id"] == w["editor"].pk)
    assert (row["access"], row["capabilities"], row["manual_only"]) == ("confined", ["ask"], False)


def test_editor_given_an_editor_line_is_full(w):
    _support(w)
    editors_run(w["agent"], manual_only=False)
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.manual_only) == (access.FULL, False)


def test_an_editor_whose_user_line_has_no_capability_to_use_is_refused(w):
    publish(w["agent"], {"capabilities": {"ask": {"role": "user", "tools": ["Read"]}}}, editors=False)
    _editors_are_users(w["agent"], roles.VIEWER)                   # a viewer line cannot use it
    assert access.decide(w["agent"], w["editor"], verified=True).access == access.NONE


def test_editors_who_run_the_agent_are_full_and_manual(w):
    publish(w["agent"], {"capabilities": ASK})
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.manual_only) == (access.FULL, True)


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_owner_and_admins_unaffected(w, who):
    _support(w)
    d = access.decide(w["agent"], w[who], verified=True)
    assert d.access == access.FULL and d.manual_only is False and d.may_request_auto is True


def test_viewer_unaffected_by_the_editor_line(w):
    publish(w["agent"], {"capabilities": ASK}, editors=False)
    before = access.decide(w["agent"], w["viewer"], verified=True)
    _editors_are_users(w["agent"])
    assert access.decide(w["agent"], w["viewer"], verified=True) == before
    assert before.access == access.CONFINED and before.capability == "ask"
