"""Forking a chat session from a message (`POST /canopy-sessions/{id}/fork`).

A fork is a new session holding the conversation up to the end of the clicked
message's turn, on the same runner and in the same worktree. The server copies
the rows and tells the runner, through `resolve_session`, how many transcript
records to copy on the fork's first turn.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness import services as harness_services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from canopy_transcript import BLOCK_STRIDE as S

pytestmark = pytest.mark.django_db

SESSION_CAPABLE = {"sessions": True, "projects": ["canopy-web"]}


def _ctx(engine=Runner.ENGINE_CLAUDE_CLI, kind=Runner.EMDASH):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", workspace=ws, kind=kind, location=Runner.LOCAL, status=Runner.ONLINE,
        owner=user, host="jj@mbp", capabilities=SESSION_CAPABLE, engine=engine,
    )
    c = Client()
    c.force_login(user)
    return user, ws, runner, c


# Two turns: record 0 prompt, 1-3 the reply; record 4 prompt, 5 the reply.
ROWS = [(0, Message.USER), (1 * S, Message.ASSISTANT), (2 * S, Message.TOOL_USE),
        (3 * S, Message.TOOL_RESULT), (3 * S + 1, Message.ASSISTANT),
        (4 * S, Message.USER), (5 * S, Message.ASSISTANT)]


def _session(ws, runner, user, *, offset=0):
    s = Session.objects.create(workspace=ws, project="canopy-web", title="widget design",
                               created_by=user, metadata={chat.TRANSCRIPT_SOURCED: True})
    RunnerBinding.objects.create(session=s, runner=runner, session_key="src-sid",
                                 emdash_project="canopy-web", host=runner.host,
                                 thread_key=str(s.id), index_offset=offset)
    for i, role in ROWS:
        Message.objects.create(session=s, turn_index=offset + i, role=role, plaintext=f"row {i}")
    return s


def _fork(c, s, turn_index):
    return c.post(f"/api/canopy-sessions/{s.id}/fork", {"turn_index": turn_index},
                  content_type="application/json")


def test_fork_keeps_the_clicked_turn_and_drops_what_follows():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user)

    r = _fork(c, s, 2 * S)  # a tool call inside the first turn

    assert r.status_code == 200, r.content
    fork = Session.objects.get(pk=r.json()["id"])
    assert fork.title == "Fork of widget design"
    assert fork.parent_session_id == s.id
    assert fork.agent_id == s.agent_id and fork.workspace_id == s.workspace_id
    assert list(fork.messages.values_list("turn_index", flat=True)) == [0, S, 2 * S, 3 * S, 3 * S + 1]
    assert fork.metadata[chat.FORK_KEY] == {"session_key": "src-sid", "records": 4,
                                            "from_session": str(s.id)}
    assert fork.metadata["requested_runner_id"] == str(runner.id)
    # The source is untouched.
    assert s.messages.count() == len(ROWS)


def test_fork_from_the_last_turn_keeps_everything():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user)

    fork = Session.objects.get(pk=_fork(c, s, 5 * S).json()["id"])

    assert fork.messages.count() == len(ROWS)
    assert fork.metadata[chat.FORK_KEY]["records"] == 6


def test_resolve_session_hands_the_cut_to_the_runner_until_the_fork_has_its_own():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user)
    fork = Session.objects.get(pk=_fork(c, s, 0).json()["id"])

    plan = harness_services.resolve_session(None, str(fork.id), runner, project="canopy-web",
                                            workspace=ws)
    assert plan["fork"] == {"session_key": "src-sid", "records": 4}
    assert plan["reuse"] is False

    RunnerBinding.objects.create(session=fork, runner=runner, session_key="fork-sid",
                                 emdash_project="canopy-web", host=runner.host,
                                 thread_key=str(fork.id))
    plan = harness_services.resolve_session(None, str(fork.id), runner, project="canopy-web",
                                            workspace=ws)
    assert plan["fork"] is None
    assert plan["reuse"] is True and plan["session_key"] == "fork-sid"
    # An ordinary chat never carries one.
    assert harness_services.resolve_session(None, str(s.id), runner, project="canopy-web",
                                            workspace=ws)["fork"] is None


def test_fork_after_a_transfer_rebases_onto_the_current_transcript():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user, offset=10 * S)
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="old box")

    fork = Session.objects.get(pk=_fork(c, s, 10 * S + S).json()["id"])

    assert list(fork.messages.values_list("turn_index", flat=True))[:2] == [0, S]
    assert not fork.messages.filter(plaintext="old box").exists()
    assert _fork(c, s, 0).status_code == 422  # before the transfer: not on this box


def test_fork_refuses_a_cut_inside_a_running_turn():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user)
    harness_services.enqueue_turn(session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                  idempotency_key="t1", prompt="go",
                                  origin_ref={"chat_session_id": str(s.id)})

    assert _fork(c, s, 5 * S).status_code == 409
    assert _fork(c, s, 0).status_code == 200  # an earlier turn is finished


@pytest.mark.parametrize("engine,kind", [
    (Runner.ENGINE_EMDASH, Runner.EMDASH),
    (Runner.ENGINE_CLAUDE_DESKTOP, Runner.EMDASH),
    (Runner.ENGINE_CLAUDE_CLI, Runner.CLOUD),
])
def test_fork_needs_a_claude_cli_runner(engine, kind):
    user, ws, runner, c = _ctx(engine=engine, kind=kind)
    s = _session(ws, runner, user)

    r = _fork(c, s, 0)

    assert r.status_code == 422
    assert not Session.objects.exclude(pk=s.pk).exists()


def test_fork_404s_an_unknown_message_and_a_stranger():
    user, ws, runner, c = _ctx()
    s = _session(ws, runner, user)
    assert _fork(c, s, 7).status_code == 404

    stranger = User.objects.create_user("x", "x@else.com", "pw")
    other = Client()
    other.force_login(stranger)
    assert _fork(other, s, 0).status_code == 404
