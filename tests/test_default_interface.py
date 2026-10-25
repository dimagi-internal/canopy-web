"""Every agent has an interface: the default, until its owner changes it.

The default is "owner, admins and editors only" — what an absent interface always
meant — now written out, so the page can say so and `GET` never returns `{}`.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import access_summary, default_interface, is_default, parse, validate
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
ACE = json.loads((pathlib.Path(__file__).parent.parent / "apps/agents/tests/fixtures/ace_interface.json").read_text())


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
    assert [line["who"] for line in got["summary"]] == ["Owner and admins", "Workspace editors", "Everyone else"]
    assert got["summary"][-1]["reaches"] == "refused"


def test_an_unknown_contact_emailing_an_agent_on_the_default_is_still_refused(w):
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                                 origin_ref={"from": "stranger@elsewhere.org"})
    assert t.status == Turn.CANCELLED and "published no interface" in t.result_note


def test_the_backfill_gives_empty_agents_the_default_and_leaves_published_ones(w):
    published = Agent.objects.create(slug="ace", name="Ace", workspace=w["agent"].workspace, interface=ACE)
    Agent.objects.filter(pk=w["agent"].pk).update(interface={})
    mig = importlib.import_module("apps.agents.migrations.0091_default_interface")

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
        "ask": {"callers": ["contact"], "tool": ["Read"]},
        "site": {"callers": ["member"], "ceiling": ["x*"]},
        "fine": {"callers": ["member"]}}}
    r = w["c"].post("/api/agents/echo/interface/validate", {"interface": draft}, content_type="application/json")
    assert r.status_code == 200
    paths = {e["path"] for e in r.json()["errors"]}
    assert paths == {"full", "ask", "site.ceiling"}
    assert validate(draft) == r.json()["errors"]


def test_validate_a_good_draft_returns_the_summary_and_saves_nothing(w):
    r = w["c"].post("/api/agents/echo/interface/validate",
                    {"interface": {"capabilities": {"ask": {"callers": ["member@dimagi.com"]}}}},
                    content_type="application/json")
    assert r.json()["errors"] == []
    lines = {line["who"]: line["reaches"] for line in r.json()["summary"]}
    assert lines["Workspace members at dimagi.com"] == "ask"
    w["agent"].refresh_from_db()
    assert is_default(w["agent"].interface)


def test_summary_says_what_each_class_reaches():
    lines = {line["who"]: line["reaches"] for line in access_summary(ACE)}
    assert lines["Verified contacts at dimagi.com"] == "everything"
    assert lines["Workspace members at dimagi.com"] == "everything"
    assert lines["Contacts"] == "ask, connect"
    assert lines["Workspace members"] == "ask, canopy, connect"
    assert lines["Everyone else"] == "refused"


def test_validate_hands_back_the_model_and_its_yaml_so_the_form_and_yaml_can_switch(w):
    import yaml
    src = "capabilities:\n  ask:\n    callers: [member]  # colleagues\n"
    r = w["c"].post("/api/agents/echo/interface/validate", {"source": src}, content_type="application/json").json()
    assert r["yaml"].strip() == src.strip() and r["interface"]["capabilities"]["ask"]["callers"] == ["member"]
    back = w["c"].post("/api/agents/echo/interface/validate", {"interface": r["interface"]},
                       content_type="application/json").json()
    assert yaml.safe_load(back["yaml"]) == r["interface"]
