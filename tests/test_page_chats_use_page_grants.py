"""A chat held on a site's page reaches that site only as the person who sent — owners
included (access.md → "A chat on a page reaches its site only as the person").

The agent's owner (or an admin) chatting with it on a connect-labs page used to run in
the agent's full profile with no capability, so the gateway refused it ("site tools
exist only in a visitor's session") and the agent fell back to its OWN labs login —
reading whatever that account could, not what the person on the page may see. Now:

* the gateway serves a full-profile turn too, through the SENDER's grant for the page,
  found from the chat key the runner gave only that session (never from an argument);
* the agent's own login at the site (`own_login:`) is named in the claim
  (`page_site.withheld`) so the runner leaves it out of the turn, and the caller
  envelope tells the agent where it stands.

Driven through the mounted canopy MCP server against a real FastMCP host, as
`test_site_gateway.py` is; every refusal asserts the host was never called.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User

from apps.agents.interface import InterfaceError, own_login_of, page_sites, parse, site_ceiling
from apps.agents.models import Agent
from apps.canopy_sessions import chat_keys
from apps.canopy_sessions.models import Session
from apps.harness import caller_tokens
from apps.harness.claiming import claim_payload
from apps.harness.models import Turn
from apps.host_grants import host_gateway
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db

IFACE = {"capabilities": {
    "connect": {"callers": ["contact"], "sites": ["connect-labs"],
                "ceiling": ["mcp__*connect_labs__marketplace_*"],
                "own_login": {"connect-labs": ["LABS_MCP_TOKEN"]},
                "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


@pytest.fixture()
def host(monkeypatch):
    return sg.host_mcp(monkeypatch)


@pytest.fixture()
def w():
    w = sg.world()
    w["agent"].interface = parse(IFACE)
    w["agent"].save(update_fields=["interface"])
    # The OWNER's own chat with the agent, held on a connect-labs page.
    w["chat"] = Session.objects.create(workspace=w["ws"], agent=w["agent"],
                                       metadata={"embed_app": "connect-labs"},
                                       page_state={"resource": "labs-run://kmc/1"})
    w["owner_turn"] = Turn.objects.create(chat_session=w["chat"], prompt="what is on this run?",
                                          initiator_user=w["owner"], initiator_kind="user", idempotency_key=str(uuid.uuid4()))
    return w


def _owner_grant(w, user=None, **over):
    user = user or w["owner"]
    return sg.grant(w, subject=f"sub-{user.pk}", contact=None, user=user, **over)


@contextlib.contextmanager
def _owner_session(w, chat=None, user=None):
    """The owner's turn as the runner drives it: the agent's own login (here a PAT)
    plus the chat key canopy minted with the claim."""
    raw, _ = PersonalToken.create_for_user(user=user or w["owner"], label="agent login")
    key = chat_keys.mint(chat or w["chat"])
    with sg.as_token(raw), mock.patch("apps.mcp.chat_scope.get_http_headers",
                                      return_value={chat_keys.HEADER.lower(): key}):
        yield


def _refused(w, host, match, args=None):
    with _owner_session(w), pytest.raises(Exception, match=match):
        sg.call(host, "site_call", args or {"tool": "marketplace_orgs_get"})
    assert host.calls == [], "a refused call must not reach the host by any route"


# --- the owner's page chat goes through the owner's own grant ---------------------------


def test_the_owners_page_chat_reaches_the_site_through_the_owners_grant(w, host):
    _owner_grant(w)
    with _owner_session(w):
        out = sg.call(host, "site_call", {"tool": "marketplace_orgs_get"}).structured_content
    assert out["is_error"] is False and host.calls == ["marketplace_orgs_get"]
    # As the gateway always calls: the person's DPoP-bound token, the agent named.
    assert host.seen and all(h["authorization"] == f"DPoP {sg.HOST_TOKEN}" for h in host.seen)
    assert all(h.get("canopy-actor") == "ace" for h in host.seen)


def test_with_no_grant_the_owners_page_chat_is_refused_never_given_a_fallback(w, host):
    _refused(w, host, "back on the page")


def test_another_persons_grant_is_never_the_owners(w, host):
    other = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    _owner_grant(w, user=other)
    _refused(w, host, "back on the page")


def test_a_grant_bound_to_a_different_agent_is_not_this_agents(w, host):
    labsdata = Agent.objects.create(slug="labsdata", name="Labs data", workspace=w["ws"],
                                    owner=w["owner"])
    _owner_grant(w, agent=labsdata)
    _refused(w, host, "back on the page")


def test_an_agent_not_set_up_for_the_site_is_refused(w, host):
    _owner_grant(w)
    w["agent"].interface = parse({"capabilities": {
        "ask": {"callers": ["contact"], "tools": ["mcp__*canopy-web__who_is_asking"]}}})
    w["agent"].save(update_fields=["interface"])
    _refused(w, host, "not set up to use")


def test_the_owners_ceiling_still_narrows_a_full_turn(w, host):
    _owner_grant(w)
    _refused(w, host, "not something you may use", args={"tool": "admin_delete_everything"})


def test_a_chat_not_held_on_a_page_has_no_site(w, host):
    _owner_grant(w)
    Session.objects.filter(pk=w["chat"].pk).update(metadata={})
    _refused(w, host, "not held on a connected site")


def test_a_pat_with_no_chat_key_is_refused(w, host):
    _owner_grant(w)
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="t")
    with sg.as_token(raw), pytest.raises(Exception, match="held on a site's page"):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == []


def test_the_chat_key_names_only_its_own_chat(w, host):
    """A key for ANOTHER chat (with no page) reaches nothing of this one."""
    _owner_grant(w)
    elsewhere = Session.objects.create(workspace=w["ws"], agent=w["agent"])
    Turn.objects.create(chat_session=elsewhere, prompt="x", initiator_user=w["owner"],
                        initiator_kind="user", idempotency_key=str(uuid.uuid4()))
    with _owner_session(w, chat=elsewhere), pytest.raises(Exception, match="not held on"):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == []


# --- an editor's turn: a read-only scoped session may still use the gateway -------------


def test_an_editors_page_chat_reaches_the_site_as_the_editor(w, host):
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=w["ws"],
                                       role=WorkspaceMembership.EDITOR)
    chat = Session.objects.create(workspace=w["ws"], agent=w["agent"],
                                  metadata={"embed_app": "connect-labs"})
    turn = Turn.objects.create(chat_session=chat, prompt="x", initiator_user=editor,
                               initiator_kind="user", idempotency_key=str(uuid.uuid4()))
    assert caller_tokens.is_caller_scoped(turn)
    _owner_grant(w, user=editor)
    with sg.as_token(caller_tokens.mint(turn, scoped=True)):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
        assert {"site_tools", "site_call", "site_read"} <= names
        out = sg.call(host, "site_call", {"tool": "marketplace_orgs_get"}).structured_content
    assert out["is_error"] is False and host.calls == ["marketplace_orgs_get"]


# --- what the runner and the agent are told ---------------------------------------------


def test_the_claim_names_the_site_and_the_agents_own_login_to_withhold(w):
    out = claim_payload(Turn.objects.get(pk=w["owner_turn"].pk))
    assert out["page_site"] == {"site": "connect-labs", "withheld": ["LABS_MCP_TOKEN"]}
    site = out["caller_context"]["site"]
    assert site["site"] == "connect-labs" and site["withheld"] == ["LABS_MCP_TOKEN"]
    assert "site_call" in site["note"] and "not available" in site["note"]


def test_a_confined_visitors_claim_names_it_too(w):
    out = claim_payload(Turn.objects.get(pk=w["turn"].pk))
    assert out["page_site"] == {"site": "connect-labs", "withheld": ["LABS_MCP_TOKEN"]}


def test_a_chat_off_any_page_withholds_nothing(w):
    Session.objects.filter(pk=w["chat"].pk).update(metadata={})
    out = claim_payload(Turn.objects.get(pk=w["owner_turn"].pk))
    assert out["page_site"] is None and out["caller_context"]["site"] is None


def test_a_site_the_agent_does_not_serve_withholds_nothing(w):
    Session.objects.filter(pk=w["chat"].pk).update(metadata={"embed_app": "elsewhere"})
    out = claim_payload(Turn.objects.get(pk=w["owner_turn"].pk))
    assert out["page_site"] is None


# --- the interface declares it ----------------------------------------------------------


def test_own_login_parses_and_answers_per_site():
    iface = parse(IFACE)
    agent = Agent(interface=iface)
    assert page_sites(agent) == {"connect-labs"}
    assert own_login_of(agent, "connect-labs") == ["LABS_MCP_TOKEN"]
    assert own_login_of(agent, "elsewhere") == []
    assert site_ceiling(agent, "connect-labs") == ["mcp__*connect_labs__marketplace_*"]
    # An interface published before the key existed parses exactly as it did.
    assert "own_login" not in parse(sg.IFACE)["capabilities"]["connect"]


def test_a_site_any_capability_opens_unnarrowed_is_unnarrowed_for_a_full_turn():
    agent = Agent(interface=parse({"capabilities": {
        "a": {"callers": ["contact"], "sites": ["connect-labs"], "ceiling": ["x_*"]},
        "b": {"callers": ["member"], "sites": ["connect-labs"]}}}))
    assert site_ceiling(agent, "connect-labs") == []


@pytest.mark.parametrize("own,match", [
    ({"elsewhere": ["X"]}, "not one of this capability's `sites:`"),
    ({"connect-labs": ["NOT A NAME"]}, "not a credential name"),
    (["LABS_MCP_TOKEN"], "must be a mapping"),
])
def test_own_login_refuses_nonsense(own, match):
    with pytest.raises(InterfaceError, match=match):
        parse({"capabilities": {"c": {"callers": ["contact"], "sites": ["connect-labs"],
                                      "own_login": own}}})


def test_the_gateway_resolves_a_full_turn_to_the_site(w):
    _owner_grant(w)
    ctx = host_gateway.resolve(str(w["owner_turn"].pk))
    assert ctx.site == "connect-labs" and ctx.ceiling == ["mcp__*connect_labs__marketplace_*"]
