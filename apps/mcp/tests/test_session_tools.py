"""Stored-session maintenance over MCP: scoped, dry-run by default, auditable.

These tools delete chat history, so the tests here are mostly about what they
must REFUSE to do: reach another workspace's rows, delete without being asked
twice, or fall back to global scope when the caller can't be resolved.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import (
    AuthenticatedUser,
    auth_context_var,
)

from apps.canopy_sessions.models import Message, Session
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()

SKILL_BODY = "Base directory for this skill: /Users/jj/.claude/plugins/cache/superpowers/…"


@contextlib.contextmanager
def as_user(user):
    """Run the block with `user` set as the authenticated MCP caller."""
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


def _workspace(slug, user, *, member=True):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=user)
    if member:
        WorkspaceMembership.objects.create(
            user=user, workspace=ws, role=WorkspaceMembership.OWNER
        )
    return ws


def _session_with(ws, rows):
    session = Session.objects.create(
        workspace=ws, origin=Session.ORIGIN_RUNNER, title="s"
    )
    for i, (role, text) in enumerate(rows):
        Message.objects.create(session=session, turn_index=i, role=role, plaintext=text)
    return session


def _call(tool, args):
    # A dict-returning tool's structured_content IS the dict (only list returns
    # get wrapped under "result").
    return async_to_sync(mcp.call_tool)(tool, args).structured_content


@pytest.mark.django_db
def test_tools_are_registered():
    tools = async_to_sync(mcp.list_tools)()
    assert {"audit_session_noise", "purge_session_noise"} <= {t.name for t in tools}


@pytest.mark.django_db
def test_audit_finds_the_skill_body_and_leaves_it_alone():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    _session_with(ws, [
        (Message.USER, "go look at the fleet"),
        (Message.ASSISTANT, "on it"),
        (Message.USER, SKILL_BODY),
    ])

    with as_user(user):
        report = _call("audit_session_noise", {})

    assert report["matched"] == 1
    assert report["sessions"] == 1
    assert report["sample"][0]["text"].startswith("Base directory for this skill:")
    assert report["sample"][0]["workspace"] == "canopy"
    assert Message.objects.count() == 3  # read-only


@pytest.mark.django_db
def test_purge_is_a_dry_run_until_told_twice():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    _session_with(ws, [(Message.USER, SKILL_BODY), (Message.ASSISTANT, "on it")])

    with as_user(user):
        dry = _call("purge_session_noise", {})
    assert dry == {**dry, "applied": False, "deleted": 0}
    assert dry["matched"] == 1
    assert Message.objects.count() == 2

    with as_user(user):
        done = _call("purge_session_noise", {"apply": True})
    assert done["applied"] is True
    assert done["deleted"] == 1
    # The sample is captured BEFORE the delete — a result that described an empty
    # table would be useless as a record of what went.
    assert done["sample"][0]["text"].startswith("Base directory for this skill:")
    assert list(Message.objects.values_list("plaintext", flat=True)) == ["on it"]


@pytest.mark.django_db
def test_another_workspaces_rows_are_invisible_and_untouchable():
    """The management command sweeps every tenant. The MCP surface must not —
    this is the whole reason the scoped path exists."""
    mine = User.objects.create_user(username="jj", email="jj@dimagi.com")
    theirs = User.objects.create_user(username="amie", email="amie@dimagi.com")
    _session_with(_workspace("mine", mine), [(Message.USER, SKILL_BODY)])
    _session_with(_workspace("theirs", theirs), [(Message.USER, SKILL_BODY)])

    with as_user(mine):
        report = _call("purge_session_noise", {"apply": True})

    assert report["matched"] == 1 and report["deleted"] == 1
    remaining = Message.objects.select_related("session__workspace")
    assert [m.session.workspace.slug for m in remaining] == ["theirs"]


@pytest.mark.django_db
def test_an_unresolvable_caller_sees_nothing_rather_than_everything():
    """Scope fails CLOSED. If the user can't be resolved the scope is empty, and
    an empty scope must match no rows — never degrade to global."""
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    _session_with(_workspace("canopy", user, member=False), [(Message.USER, SKILL_BODY)])

    stranger = User.objects.create_user(username="nobody", email="nobody@example.com")
    with as_user(stranger):
        report = _call("purge_session_noise", {"apply": True})

    assert report["matched"] == 0
    assert report["deleted"] == 0
    assert Message.objects.count() == 1


@pytest.mark.django_db
def test_a_single_session_can_be_targeted():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    keep = _session_with(ws, [(Message.USER, SKILL_BODY)])
    target = _session_with(ws, [(Message.USER, SKILL_BODY)])

    with as_user(user):
        report = _call("purge_session_noise", {"session_id": str(target.id), "apply": True})

    assert report["deleted"] == 1
    assert list(Message.objects.values_list("session_id", flat=True)) == [keep.id]


@pytest.mark.django_db
def test_a_real_message_is_never_matched():
    """Prefix-anchored: a human ASKING about a skill's base directory is talking."""
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    _session_with(ws, [(Message.USER, "what is the base directory for this skill?")])

    with as_user(user):
        report = _call("purge_session_noise", {"apply": True})

    assert report["matched"] == 0
    assert Message.objects.count() == 1


@pytest.mark.django_db
def test_an_assistant_row_quoting_a_marker_survives():
    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    _session_with(ws, [(Message.ASSISTANT, SKILL_BODY)])

    with as_user(user):
        report = _call("purge_session_noise", {"apply": True})

    assert report["matched"] == 0
    assert Message.objects.count() == 1


@pytest.mark.django_db
def test_the_sample_is_bounded_however_much_is_asked_for():
    """The report travels into a model's context; an unbounded sample is a
    denial of service against whoever reads it."""
    from apps.canopy_sessions import maintenance

    user = User.objects.create_user(username="jj", email="jj@dimagi.com")
    ws = _workspace("canopy", user)
    _session_with(ws, [(Message.USER, SKILL_BODY) for _ in range(80)])

    with as_user(user):
        report = _call("audit_session_noise", {"sample": 1000})

    assert report["matched"] == 80
    assert len(report["sample"]) == maintenance.SAMPLE_MAX
