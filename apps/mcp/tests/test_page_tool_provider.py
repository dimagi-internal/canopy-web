"""The page's actions reach the agent through the REAL server, or they reach nobody.

`page_tools.py` was written, documented and unit-tested in full — and never
imported by `server.py`. Every one of its own tests passed while the feature it
implements did not exist: an agent asking canopy for its tools got the static
list and nothing else, so "close all the insights" had no tool to call.

That is the gap these tests close. They go through `mcp.list_tools()` and
`mcp.call_tool()` — the same entry points a real MCP client hits — rather than
through the module, because the defect was never inside the module.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.canopy_sessions import page_actions
from apps.canopy_sessions.models import PageAction, Session
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()

pytestmark = pytest.mark.django_db

DISMISS = {
    "name": "dismissInsights",
    "description": "Dismiss insights from the list the user is viewing",
    "inputSchema": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["ids"],
    },
}


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(
        token="test-token",
        client_id=str(user.pk),
        scopes=["canopy:user"],
        claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _user_with_page(name="jj", actions=(DISMISS,)):
    user = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    ws, _ = Workspace.objects.get_or_create(
        slug="w1", defaults={"display_name": "W1", "created_by": user}
    )
    WorkspaceMembership.objects.get_or_create(
        user=user, workspace=ws, defaults={"role": WorkspaceMembership.OWNER}
    )
    session = Session.objects.create(
        workspace=ws, created_by=user, title="chat", page_actions_available=list(actions)
    )
    return user, session


def test_an_open_page_puts_its_action_in_the_agents_tool_list():
    """THE test. The module was complete and the server never asked it anything."""
    user, _session = _user_with_page()

    with as_user(user):
        tools = async_to_sync(mcp.list_tools)()

    tool = next((t for t in tools if t.name == "page_dismissInsights"), None)
    assert tool is not None, f"page tools never reached the server: {sorted(t.name for t in tools)}"
    # And with the host's schema, which is the only way the agent knows the
    # call takes `ids`.
    assert tool.parameters["required"] == ["ids"]


def test_the_static_tools_are_still_there():
    """A provider must ADD to the surface, not replace it."""
    user, _session = _user_with_page()
    with as_user(user):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"list_insights", "clear_insights"} <= names


def test_another_users_page_is_not_in_my_tool_list():
    """The list is computed per caller; a page belongs to whoever has it open."""
    mine, _ = _user_with_page("jj")
    _theirs, _ = _user_with_page("other")

    with as_user(mine):
        mine_names = {t.name for t in async_to_sync(mcp.list_tools)()}

    # Both users declared the same action, so a leak would be invisible by name
    # alone — assert on the SESSION named in the description instead.
    tool = next(t for t in mine_names if t == "page_dismissInsights")
    assert tool  # present for me
    theirs_sessions = Session.objects.exclude(created_by=mine)
    with as_user(mine):
        described = next(
            t.description for t in async_to_sync(mcp.list_tools)() if t.name == "page_dismissInsights"
        )
    for session in theirs_sessions:
        assert str(session.id) not in described


def test_an_unauthenticated_caller_gets_no_page_tools():
    """Rather than raising: the server lists tools in contexts with no caller."""
    _user, _session = _user_with_page()
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert not any(n.startswith("page_") for n in names)


def test_calling_it_queues_a_real_page_action():
    """The call has to reach `page_actions`, not merely resolve to a tool."""
    user, session = _user_with_page()

    with as_user(user):
        with pytest.raises(ToolError) as exc:
            # No page is listening in a test, so it times out — which is the
            # point: it got far enough to WAIT, and then refused out loud.
            async_to_sync(mcp.call_tool)(
                "page_dismissInsights", {"ids": [1, 2]}
            )

    assert "timeout" in str(exc.value)
    action = PageAction.objects.get()
    assert action.session_id == session.id
    assert action.name == "dismissInsights"
    assert action.args == {"ids": [1, 2]}
    # And it is recorded as unanswered rather than left pending forever.
    assert action.status == PageAction.EXPIRED


def test_a_refusal_reaches_the_model_as_words_not_an_opaque_error():
    """An agent told "the page is closed" can respond; one shown a traceback
    cannot. Arguments the page's own schema rejects never leave the server."""
    user, _session = _user_with_page()

    with as_user(user):
        with pytest.raises(ToolError) as exc:
            async_to_sync(mcp.call_tool)("page_dismissInsights", {})

    assert "bad_arguments" in str(exc.value)
    assert "ids" in str(exc.value)
    assert not PageAction.objects.exists(), "no row for a call the page would reject"


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    """Half a second, not twenty — nothing answers in a test."""
    monkeypatch.setattr(page_actions, "DEFAULT_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(page_actions, "POLL_INTERVAL_SECONDS", 0.02)
