"""A confined turn on a site page reaches that page's own actions through canopy's
`page_*` tools — in a sensitive conversation too — and `site_call` sends a page
action back to them by name instead of to the host.

The page's actions run in the person's open tab, as them (`apps/mcp/page_tools.py`).
A confined session presents its caller token, not a chat key, so the tools answer
about the conversation that token names, and nothing else. Driven through the
MOUNTED server with a real caller token.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from fastmcp.exceptions import ToolError

from apps.agents.interface import PAGE_TOOLS, parse, profile
from apps.agents.sensitive_data import PAGE_TOOL_GLOB, structural_refusals
from apps.canopy_sessions import page_actions
from apps.canopy_sessions.models import PageAction, Session
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.mcp.models import MCPAuditLog
from apps.mcp.page_tools import to_mcp_tool
from apps.mcp.server import mcp
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db

SENSITIVE = {"runner_requirements": ["sensitive_data"]}
SHOW = {"name": "showQuery", "description": "Show a query in the page's canvas",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}}}


@pytest.fixture()
def host(monkeypatch):
    return sg.host_mcp(monkeypatch)


@pytest.fixture()
def w():
    w = sg.world()
    page_actions.set_declared_actions(w["session"], [SHOW])
    # Another visitor's conversation with the same agent, whose page offers something else.
    other = Session.objects.create(workspace=w["ws"], agent=w["agent"], contact=w["contact"],
                                   metadata={"embed_app": "connect-labs"})
    page_actions.set_declared_actions(other, [{"name": "openItem"}])
    w["other"] = other
    return w


def _sensitive(w):
    caps = dict(sg.IFACE["capabilities"])
    caps["connect"] = {**caps["connect"], "data": "sensitive"}
    w["agent"].interface = parse({"capabilities": caps})
    w["agent"].save(update_fields=["interface"])
    for s in (w["session"], w["other"]):
        s.metadata = {**s.metadata, **SENSITIVE}
        s.save(update_fields=["metadata"])


def _names(w):
    with sg.visitor(w):
        return {t.name for t in async_to_sync(mcp.list_tools)()}


def _page_answers(monkeypatch, result):
    monkeypatch.setattr(page_actions, "DEFAULT_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(page_actions, "POLL_INTERVAL_SECONDS", 0.01)
    real_sleep = page_actions.time.sleep

    def answer(_seconds):
        real_sleep(0)
        pending = PageAction.objects.filter(status=PageAction.PENDING).first()
        if pending is not None:
            page_actions.resolve(pending, result=result)
    monkeypatch.setattr(page_actions.time, "sleep", answer)


# --- the profile and the listing -----------------------------------------------------


def test_a_site_capability_carries_the_page_tools(w):
    assert set(PAGE_TOOLS) <= set(profile(w["agent"], "connect")["tools"])
    # A capability with no site gains nothing.
    assert not set(PAGE_TOOLS) & set(profile(w["agent"], "ask")["tools"])


def test_a_confined_visitor_sees_its_own_pages_actions_and_no_one_elses(w):
    names = _names(w)
    assert "page_showQuery" in names and "page_openItem" not in names


@pytest.mark.parametrize("sensitive", [False, True])
def test_the_turn_runs_its_pages_action_and_gets_the_answer(w, monkeypatch, sensitive):
    if sensitive:
        _sensitive(w)
    _page_answers(monkeypatch, {"shown": True})
    with sg.visitor(w):
        out = async_to_sync(mcp.call_tool)("page_showQuery", {"sql": "select 1"})
    assert out.structured_content == {"result": {"shown": True}}
    row = PageAction.objects.get(session=w["session"])
    assert row.status == PageAction.DONE
    if sensitive:
        assert (row.args, row.result) == ({}, None)      # nothing kept at rest


def test_a_sensitive_turn_lists_its_pages_actions_and_nothing_wider(w):
    _sensitive(w)
    grant = caller_tokens.resolve(caller_tokens.mint(w["turn"]))
    assert grant.sensitive and PAGE_TOOL_GLOB in grant.tool_globs
    names = _names(w)
    assert "page_showQuery" in names and "page_openItem" not in names
    assert not {"create_turn", "list_page_actions", "invoke_page_action"} & names


def test_another_conversations_page_tool_is_refused_to_this_token(w):
    _sensitive(w)
    tool = to_mcp_tool("page_openItem", w["other"], {"name": "openItem"})
    with sg.visitor(w), pytest.raises(ToolError) as err:
        async_to_sync(tool.run)({})
    assert "sensitive" in str(err.value)
    assert not PageAction.objects.filter(session=w["other"]).exists()


def test_a_capability_that_does_not_list_page_tools_does_not_reach_them(w):
    t = Turn.objects.create(chat_session=w["session"], prompt="q", initiator_contact=w["contact"],
                            initiator_kind="contact", capability="ask", idempotency_key="ask-1")
    with sg.as_token(caller_tokens.mint(t)):
        names = {x.name for x in async_to_sync(mcp.list_tools)()}
    assert not any(n.startswith("page_") for n in names)


def test_a_sensitive_capability_may_name_the_page_tools_and_nothing_broader():
    cap = {"sites": ["labs"], "tools": ["mcp__*canopy-web__page_*"]}
    assert structural_refusals("data", cap) == []
    assert structural_refusals("data", {"sites": ["labs"], "tools": ["mcp__*canopy-web__*"]})


# --- site_call sends a page action back ----------------------------------------------


def test_site_call_refuses_a_page_action_by_name_and_points_at_the_page_tool(w, host):
    sg.grant(w)
    with sg.visitor(w), pytest.raises(ToolError) as err:
        sg.call(host, "site_call", {"tool": "page_showQuery", "arguments": {"sql": "select 1"}})
    msg = str(err.value)
    assert "`page_showQuery`" in msg and "not one of the site's tools" in msg
    assert host.calls == [] and not host.seen
    assert MCPAuditLog.objects.filter(tool="site_call", ok=False, error="page_action").exists()


def test_site_call_leaves_a_page_named_tool_this_page_does_not_declare_to_the_gateway(w, host):
    # `page_openItem` is the OTHER conversation's action, not this one's: the gateway's
    # own rules answer it (here the owner's ceiling), not the page-action pointer.
    sg.grant(w)
    with sg.visitor(w), pytest.raises(ToolError) as err:
        sg.call(host, "site_call", {"tool": "page_openItem", "arguments": {}})
    assert "not something you may use" in str(err.value)
    assert not MCPAuditLog.objects.filter(tool="site_call", error="page_action").exists()


# --- the wait never holds the doorbell up ---------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_wait_leaves_the_shared_sync_thread_free_for_the_doorbell(monkeypatch):
    """The page's doorbell is delivered by the chat socket, whose database work runs on
    the shared (thread-sensitive) sync thread. The tool's 20-second wait for the answer
    ran there too, so the doorbell reached the page only when the wait expired: the
    action ran, answered at once, and found its row expired (labs Data page, prod)."""
    import asyncio

    from asgiref.sync import sync_to_async

    w = sg.world()
    page_actions.set_declared_actions(w["session"], [SHOW])
    monkeypatch.setattr(page_actions, "DEFAULT_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(page_actions, "POLL_INTERVAL_SECONDS", 0.05)

    def doorbell_then_page_answers():
        # What the socket does with the frame, on the thread-sensitive executor: find
        # the pending row; the page answers it.
        pending = PageAction.objects.filter(session=w["session"], status=PageAction.PENDING).first()
        if pending is None:
            return False
        page_actions.resolve(pending, result={"shown": True})
        return True

    async def go():
        call = asyncio.ensure_future(mcp.call_tool("page_showQuery", {"sql": "select 1"}))
        answered = False
        for _ in range(40):
            await asyncio.sleep(0.05)
            answered = await asyncio.wait_for(
                sync_to_async(doorbell_then_page_answers, thread_sensitive=True)(), timeout=1)
            if answered:
                break
        return answered, await call

    with sg.visitor(w):
        answered, out = async_to_sync(go)()
    assert answered
    assert out.structured_content == {"result": {"shown": True}}
