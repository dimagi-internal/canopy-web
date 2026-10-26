"""`editors: confined` — an interface that sends workspace editors through the
capabilities, like any viewer member, instead of the editor tier (full, manual).
docs/architecture/access.md § The rule; the door-by-door matrix is in
tests/test_access_matrix.py (`eva_confined` / `hal_confined`)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import access
from apps.agents.interface import InterfaceError, access_summary, parse, validate
from apps.agents.models import Agent, AgentAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership as M

ASK = {"ask": {"callers": ["member"], "tools": ["Read"]}}
SUPPORT = {"editors": "confined", "capabilities": ASK}


def test_default_is_unchanged_and_never_written():
    assert "editors" not in parse({"capabilities": ASK})
    assert "editors" not in parse({"editors": "full", "capabilities": ASK})
    assert parse(SUPPORT)["editors"] == "confined"


@pytest.mark.parametrize("value", ["confine", "manual", "", None, True, ["confined"]])
def test_unknown_values_are_refused_at_publish(value):
    with pytest.raises(InterfaceError) as exc:
        parse({"editors": value, "capabilities": ASK})
    assert exc.value.path == "editors"
    assert validate({"editors": value, "capabilities": ASK})[0]["path"] == "editors"


def test_confined_needs_something_to_confine_to():
    with pytest.raises(InterfaceError):
        parse({"editors": "confined"})
    assert parse({"editors": "confined", "full": ["member"]})["editors"] == "confined"


def test_the_summary_says_editors_reach_the_capabilities():
    lines = {r["who"]: r["reaches"] for r in access_summary(parse(SUPPORT))}
    assert "ask" in lines["Workspace editors"] and "everything" not in lines["Workspace editors"]
    default = {r["who"]: r["reaches"] for r in access_summary(parse({"capabilities": ASK}))}
    assert default["Workspace editors"].startswith("everything")


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


def _publish(agent, doc):
    agent.interface = parse(doc)
    agent.save(update_fields=["interface"])


def test_editor_is_confined_to_ask_with_no_manual_cap(w):
    _publish(w["agent"], SUPPORT)
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.capability, d.basis) == (access.CONFINED, "ask", "capability:ask")
    assert d.manual_only is False and d.may_request_auto is False
    row = next(r for r in access.roster(w["agent"]) if r["user_id"] == w["editor"].pk)
    assert (row["access"], row["capabilities"], row["manual_only"]) == ("confined", ["ask"], False)


def test_editor_named_by_a_full_rule_is_still_full(w):
    _publish(w["agent"], {**SUPPORT, "full": ["member@dimagi.com"]})
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.basis, d.manual_only) == (access.FULL, "full:member@dimagi.com", False)


def test_editor_no_capability_lists_is_refused(w):
    _publish(w["agent"], {"editors": "confined",
                          "capabilities": {"ask": {"callers": ["contact"]}}})
    assert access.decide(w["agent"], w["editor"], verified=True).access == access.NONE


def test_default_editor_tier_unchanged(w):
    _publish(w["agent"], {"capabilities": ASK})
    d = access.decide(w["agent"], w["editor"], verified=True)
    assert (d.access, d.basis, d.manual_only) == (access.FULL, access.EDITOR_BASIS, True)


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_owner_and_admins_unaffected(w, who):
    _publish(w["agent"], SUPPORT)
    d = access.decide(w["agent"], w[who], verified=True)
    assert d.access == access.FULL and d.manual_only is False and d.may_request_auto is True


def test_viewer_unchanged(w):
    _publish(w["agent"], {"capabilities": ASK})
    before = access.decide(w["agent"], w["viewer"], verified=True)
    _publish(w["agent"], SUPPORT)
    assert access.decide(w["agent"], w["viewer"], verified=True) == before
    assert before.access == access.CONFINED and before.capability == "ask"
