"""Every agent has an interface: the default, until its owner changes it.

The default is "no profile" — owner and admins only until the access list says more —
written out, so the page can say so and a read never comes back empty.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import InterfaceError, default_interface, parse, published
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
_ACE_OLD = json.loads((pathlib.Path(__file__).parent.parent / "apps/agents/tests/fixtures/ace_interface.json").read_text())
# ACE's published interface in today's words: the `full:` rules and each profile's `callers` are
# the access list's business; a profile keeps only what it is.
# `version` and a profile's `input` are retired (accepted on a save, never written back).
ACE = {"capabilities": {
    name: {k: v for k, v in cap.items() if k not in ("callers", "input")}
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
    assert not published(w["agent"].interface)
    assert parse(default_interface()) == default_interface()


def test_an_agent_holding_nothing_reads_as_the_default(w):
    Agent.objects.filter(pk=w["agent"].pk).update(interface={})   # a row from before the default
    assert w["c"].get("/api/agents/echo/read-mode").json()["source"] == "default"
    assert w["c"].get("/api/agents/echo/connected-sites").json()["sites"] == []


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


def test_aces_published_interface_parses_to_itself_and_reads_as_a_custom_read_mode(w):
    assert parse(ACE) == ACE
    Agent.objects.filter(pk=w["agent"].pk).update(interface=ACE)
    assert w["c"].get("/api/agents/echo/read-mode").json()["source"] == "custom"


def test_going_back_to_the_default_after_a_save(w):
    put = w["c"].put("/api/agents/echo/read-mode", {"tools": ["Read"]}, content_type="application/json")
    assert put.status_code == 200 and put.json()["source"] == "custom"
    r = w["c"].delete("/api/agents/echo/read-mode")
    assert r.json()["source"] == "default"
    w["agent"].refresh_from_db()
    assert not published(w["agent"].interface)


@pytest.mark.parametrize("doc, where", [
    ({"full": ["contactt"]}, "full"),
    ({"capabilities": {"ask": {"tool": ["Read"]}}}, "ask"),
    ({"capabilities": {"site": {"ceiling": ["x*"]}}}, "site.ceiling"),
    ({"capabilities": {"old": {"callers": ["member"]}}}, "old.callers"),
])
def test_a_refusal_names_the_field_it_is_about(doc, where):
    with pytest.raises(InterfaceError) as exc:
        parse(doc)
    assert exc.value.path == where


@pytest.mark.parametrize("doc", [{"full": ["member"]}, {"editors": "confined"},
                                 {"capabilities": {"ask": {"callers": ["member"]}}}])
def test_parse_refuses_the_retired_keys_and_points_at_the_access_list(doc):
    with pytest.raises(InterfaceError, match="access list"):
        parse(doc)
