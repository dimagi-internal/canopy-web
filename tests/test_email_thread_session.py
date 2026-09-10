"""An inbound email thread becomes a Session a human can open, watch and steer.

A Turn is a fine unit of EXECUTION and a poor unit of CONVERSATION: it ends, and
leaves nothing to open or type into. A Session is what ace-web already renders
through the shared canopy chat kit, so binding an email turn to one is what turns
"ACE replied to that eventually" into "here is the run, live".

Everything here is about the binding being ADDITIVE. The turn stays an AGENT turn
— routing (including the source/actor rules), tenancy and per-agent
serialization must all be exactly what they were, or a change meant to give
someone a URL has quietly rerouted the fleet's mail.
"""
from __future__ import annotations

import uuid

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="owner", email="owner@dimagi.com", password="x")


@pytest.fixture
def workspace(owner):
    from apps.workspaces.models import Workspace, WorkspaceMembership
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(
        user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def agent(workspace):
    return Agent.objects.create(slug="ace", name="ACE", workspace=workspace)


def _email_turn(agent, thread_id, subject="Pipeline test", key=None):
    return services.enqueue_turn(
        agent=agent,
        origin=Turn.ORIGIN_EMAIL,
        idempotency_key=key or f"email:{uuid.uuid4().hex}",
        prompt="handle this thread",
        origin_ref={"from": "hal@dimagi-ai.com", "subject": subject,
                    "thread_id": thread_id, "discovered_by": "poll"},
    )


def test_an_email_turn_gets_a_session_to_watch(agent):
    turn, _ = _email_turn(agent, "1a085da485720e8a")
    assert turn.chat_session_id is not None, "nothing for a human to open"
    assert turn.chat_session.agent_id == agent.id


def test_the_same_thread_continues_the_same_session(agent):
    """A reply is the same conversation. `_thread_session` could not do this — it
    resolves a thread_key only as a Session UUID, so a non-UUID key creates a
    fresh row every time."""
    first, _ = _email_turn(agent, "thread-A")
    second, _ = _email_turn(agent, "thread-A")
    # Not-None FIRST: without it this passes vacuously when both bindings fail
    # and the assertion becomes None == None. It did exactly that on the first run.
    assert first.chat_session_id is not None
    assert first.chat_session_id == second.chat_session_id


def test_a_new_thread_opens_a_parallel_session(agent):
    a, _ = _email_turn(agent, "thread-A")
    b, _ = _email_turn(agent, "thread-B")
    assert a.chat_session_id != b.chat_session_id


def test_the_session_is_listable_by_ace_web(agent):
    """ace-web filters its session list on metadata.origin_key. Without it the
    session exists and is invisible, which is the least useful outcome available."""
    turn, _ = _email_turn(agent, "thread-C")
    meta = turn.chat_session.metadata
    assert meta.get("origin_key", "").startswith("ace-web:"), meta
    assert meta.get("source") == "email", meta


def test_the_turn_targets_the_session_and_still_names_its_agent(agent):
    """`turn_targets_agent_xor_project_xor_session` is a DB CHECK constraint, so
    a turn cannot carry both. What must survive the conversion is the agent: the
    runner drives it (`resolve_agent_slug` reads chat_session.agent.slug) and
    routing resolves the source/actor rules through it."""
    from apps.harness.schemas import TurnOut
    turn, _ = _email_turn(agent, "thread-D")
    assert turn.agent_id is None
    assert turn.chat_session.agent_id == agent.id
    assert TurnOut.resolve_agent_slug(turn) == agent.slug
    assert turn.workspace_id is None, "tenancy derives from the session, never denormalized"


def test_two_threads_are_not_serialized_behind_each_other(agent):
    """The point of keying on the thread: separate conversations run in parallel
    rather than queueing behind one agent."""
    a, _ = _email_turn(agent, "thread-P1")
    b, _ = _email_turn(agent, "thread-P2")
    assert a.chat_session_id != b.chat_session_id
    assert a.agent_id is None and b.agent_id is None


def test_a_non_email_turn_gets_no_session(agent):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API,
        idempotency_key=f"api:{uuid.uuid4().hex}", prompt="hi",
    )
    assert turn.chat_session_id is None


def test_an_email_turn_with_no_thread_id_still_runs(agent):
    """Degrade, never block: mail is not a surface that tolerates a turn failing
    to exist because its session could not be made."""
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=f"email:{uuid.uuid4().hex}", prompt="hi",
        origin_ref={"from": "x@y.z", "subject": "no thread id"},
    )
    assert turn.pk is not None
    assert turn.chat_session_id is None


def test_a_session_failure_does_not_lose_the_turn(agent, monkeypatch):
    monkeypatch.setattr(services, "email_thread_session",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db sad")))
    turn, _ = _email_turn(agent, "thread-E")
    assert turn.pk is not None, "the mail still has to be handled"
    assert turn.chat_session_id is None
