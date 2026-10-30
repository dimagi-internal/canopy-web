"""Every agent has an interface: the default, until its owner changes it.

The default is "no capability" — owner and admins only until the access list says more —
written out, so the page can say so and `GET` never returns `{}`.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import InterfaceError, default_interface, is_default, parse, validate
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
_ACE_OLD = json.loads((pathlib.Path(__file__).parent.parent / "apps/agents/tests/fixtures/ace_interface.json").read_text())
# ACE's published interface in today's words: the `full:` rules and each capability's `callers` are
# the access list's business; a capability keeps what it is and the lowest role that may use it.
ACE = {"version": 1, "capabilities": {
    name: {**{k: v for k, v in cap.items() if k != "callers"},
           "role": "viewer" if any(c.startswith("contact") for c in cap.get("callers", [])) else "user"}
    for name, cap in _ACE_OLD["capabilities"].items()}}


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=op)
    c = Client()
    c.force_login(op)
    return {"agent": agent, "c": c}


def test_a_new_agent_is_created_with_the_default(w):
    assert w["agent"].interface == default_interface()
    assert is_default(w["agent"].interface)
    assert parse(default_interface()) == default_interface()


def test_get_never_returns_an_empty_interface(w):
    Agent.objects.filter(pk=w["agent"].pk).update(interface={})   # a row from before the default
    got = w["c"].get("/api/agents/echo/interface").json()
    assert got["interface"] == default_interface() and got["is_default"] is True
    assert "summary" not in got and "warnings" not in got


def test_an_unknown_contact_emailing_an_agent_on_the_default_is_still_refused(w):
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                                 origin_ref={"from": "stranger@elsewhere.org"})
    assert t.status == Turn.CANCELLED and "access list" in t.result_note


def test_the_backfill_gives_empty_agents_the_default_and_leaves_published_ones(w):
    published = Agent.objects.create(slug="ace", name="Ace", workspace=w["agent"].workspace, interface=ACE)
    Agent.objects.filter(pk=w["agent"].pk).update(interface={})
    mig = importlib.import_module("apps.agents.migrations.0092_default_interface")

    class _Apps:
        get_model = staticmethod(lambda app, name: Agent)

    mig.give_every_agent_an_interface(_Apps, None)
    w["agent"].refresh_from_db()
    published.refresh_from_db()
    assert w["agent"].interface == default_interface()
    assert published.interface == ACE


def test_aces_published_interface_survives_a_save_unchanged(w):
    """The form saves the model it loaded: ACE's interface in, the same interface out."""
    assert parse(ACE) == ACE
    r = w["c"].put("/api/agents/echo/interface", {"interface": ACE}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["interface"] == ACE and r.json()["is_default"] is False
    assert w["c"].get("/api/agents/echo/interface").json()["interface"] == ACE
    # …and the YAML view of it is the same model again.
    import yaml
    assert parse(yaml.safe_load(r.json()["yaml"])) == ACE


def test_reset_to_default_after_a_save(w):
    w["c"].put("/api/agents/echo/interface", {"interface": ACE}, content_type="application/json")
    r = w["c"].delete("/api/agents/echo/interface")
    assert r.json()["interface"] == default_interface() and r.json()["is_default"] is True
    w["agent"].refresh_from_db()
    assert w["agent"].interface == default_interface()


def test_validate_reports_each_fault_next_to_its_field(w):
    draft = {"full": ["contactt"], "capabilities": {
        "ask": {"tool": ["Read"]},
        "site": {"role": "user", "ceiling": ["x*"]},
        "old": {"callers": ["member"]},
        "fine": {"role": "user"}}}
    r = w["c"].post("/api/agents/echo/interface/validate", {"interface": draft}, content_type="application/json")
    assert r.status_code == 200
    paths = {e["path"] for e in r.json()["errors"]}
    assert paths == {"full", "ask", "site.ceiling", "old.callers"}
    assert all("access list" in e["message"] for e in r.json()["errors"] if e["path"] in ("full", "old.callers"))
    assert validate(draft) == r.json()["errors"]


def test_validate_a_good_draft_saves_nothing(w):
    r = w["c"].post("/api/agents/echo/interface/validate",
                    {"interface": {"capabilities": {"ask": {"role": "user"}}}},
                    content_type="application/json")
    assert r.json()["errors"] == [] and "summary" not in r.json() and "warnings" not in r.json()
    w["agent"].refresh_from_db()
    assert is_default(w["agent"].interface)


@pytest.mark.parametrize("doc", [{"full": ["member"]}, {"editors": "confined"},
                                 {"capabilities": {"ask": {"callers": ["member"]}}}])
def test_parse_refuses_the_retired_keys_and_points_at_the_access_list(doc):
    with pytest.raises(InterfaceError, match="access list"):
        parse(doc)


def test_validate_hands_back_the_model_and_its_yaml_so_the_form_and_yaml_can_switch(w):
    import yaml
    src = "capabilities:\n  ask:\n    role: user  # colleagues\n"
    r = w["c"].post("/api/agents/echo/interface/validate", {"source": src}, content_type="application/json").json()
    assert r["yaml"].strip() == src.strip() and r["interface"]["capabilities"]["ask"]["role"] == "user"
    back = w["c"].post("/api/agents/echo/interface/validate", {"interface": r["interface"]},
                       content_type="application/json").json()
    assert yaml.safe_load(back["yaml"]) == r["interface"]
