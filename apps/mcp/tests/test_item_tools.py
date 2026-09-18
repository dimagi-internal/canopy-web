"""`list_items` — the read that lets the inbox send ids instead of rows.

Driven through the real FastMCP instance (`mcp.call_tool`), not the function,
because a tool that is never registered is a tool that does not exist:
`page_tools.py` shipped with ten passing tests and no import.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.harness.models import Item
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()

pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(
        token="t", client_id=str(user.pk), scopes=["canopy:user"],
        claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _workspace(slug, user):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=user)
    if user is not None:
        WorkspaceMembership.objects.create(
            user=user, workspace=ws, role=WorkspaceMembership.OWNER
        )
    return ws


def _agent(ws, slug):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def _item(agent, title, *, kind=Item.REVIEW, state=Item.OPEN):
    return Item.objects.create(
        agent=agent, title=title, kind=kind, state=state, origin="manual",
        idempotency_key=str(uuid.uuid4()),
    )


def _call(**kwargs):
    return async_to_sync(mcp.call_tool)("list_items", kwargs).structured_content["result"]


def test_the_tool_is_registered_on_the_mounted_server():
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert "list_items" in names


def test_it_returns_open_items_in_the_callers_workspaces():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    agent = _agent(_workspace("connect", user), "echo")
    _item(agent, "review the deploy")

    with as_user(user):
        rows = _call()

    assert [r["title"] for r in rows] == ["review the deploy"]
    assert rows[0]["agent"] == "echo"


def test_a_decided_item_is_not_waiting_on_anyone():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    agent = _agent(_workspace("connect", user), "echo")
    _item(agent, "still open")
    _item(agent, "already handled", state=Item.DECIDED)

    with as_user(user):
        rows = _call()

    assert [r["title"] for r in rows] == ["still open"]


def test_another_tenants_items_are_invisible():
    """The gate is the caller's workspaces, one hop away through the agent —
    an Item has no workspace column of its own."""
    mine = User.objects.create_user(username="jj", email="jj@dimagi.com")
    theirs = User.objects.create_user(username="sam", email="sam@dimagi.com")
    _item(_agent(_workspace("mine", mine), "echo"), "my item")
    _item(_agent(_workspace("theirs", theirs), "spark"), "their item")

    with as_user(mine):
        rows = _call()

    assert [r["title"] for r in rows] == ["my item"]


def test_an_unauthenticated_caller_gets_nothing_not_everything():
    """Fail closed. A missing caller resolving to "no filter" is the
    `workspace_id IS NULL means allow` bug in a different costume."""
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    _item(_agent(_workspace("connect", user), "echo"), "my item")

    assert _call() == []


def test_it_filters_by_agent_and_by_kind():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("connect", user)
    echo, spark = _agent(ws, "echo"), _agent(ws, "spark")
    _item(echo, "echo review")
    _item(echo, "echo question", kind=Item.QUESTION)
    _item(spark, "spark review")

    with as_user(user):
        assert {r["title"] for r in _call(agent="echo")} == {"echo review", "echo question"}
        assert [r["title"] for r in _call(kind=Item.QUESTION)] == ["echo question"]


def test_limit_is_clamped_so_a_page_cannot_ask_for_the_whole_table():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    agent = _agent(_workspace("connect", user), "echo")
    for n in range(5):
        _item(agent, f"item {n}")

    with as_user(user):
        assert len(_call(limit=2)) == 2
        assert len(_call(limit=10_000)) == 5  # clamped to 100, not an error
