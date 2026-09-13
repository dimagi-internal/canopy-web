"""Who may make an agent RUN.

`POST /api/harness/turns/` is the executing half of the author tier: the body
is arbitrary prompt text that a runner claims and executes as the agent,
holding the agent's resolved credentials. It was gated on bare workspace
MEMBERSHIP, so a `viewer` — the tier whose whole definition is passive reading
plus interaction — could enqueue a turn with any prompt against any agent in
their workspace and have the fleet run it.

That is the same shape the schedule surface had (`tests/test_schedule_api.py`),
reached by a different door: a schedule is a prompt that fires later, a turn is
a prompt that fires now.

Session turns are deliberately NOT gated the same way, and that distinction is
the point rather than an omission — a chat send produces a session turn, and
talking to an agent is exactly what the interaction tier is for.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

WS = "turn-acl-ws"


@pytest.fixture
def fleet():
    ws = a_workspace(WS)  # named slug -> no self_join_domains
    agent = Agent.objects.create(slug="runbot", name="Run Bot", workspace=ws)
    editor = a_member(ws, email="turn-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    viewer = a_member(ws, email="turn-viewer@dimagi.com", role=WorkspaceMembership.VIEWER)
    outsider = get_user_model().objects.create_user(
        username="turn-outsider", email="turn-outsider@example.com"
    )
    return {"ws": ws, "agent": agent, "editor": editor, "viewer": viewer, "outsider": outsider}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _enqueue(client, **over):
    body = {
        "agent_slug": "runbot",
        "prompt": "/runbot:anything",
        "origin": "api",
        "idempotency_key": over.pop("key", "k1"),
    }
    body.update(over)
    return client.post("/api/harness/turns/", body, content_type="application/json")


def test_viewer_cannot_enqueue_an_agent_turn(fleet):
    res = _enqueue(_as(fleet["viewer"]))
    assert res.status_code == 403, res.content
    assert not Turn.objects.exists(), "a refused enqueue created a turn anyway"


def test_editor_can_enqueue_an_agent_turn(fleet):
    """The gate must not break the tier it belongs to."""
    res = _enqueue(_as(fleet["editor"]))
    assert res.status_code in (200, 201), res.content
    assert Turn.objects.count() == 1


def test_non_member_gets_404_not_403(fleet):
    """Resolve-then-authorize. A non-member must not learn the agent exists."""
    res = _enqueue(_as(fleet["outsider"]))
    assert res.status_code == 404, res.content


def test_viewer_cannot_cancel_an_agent_turn(fleet):
    """Withdrawing is the same tier as dispatching.

    A viewer who cannot dispatch an agent turn has nothing to withdraw, so
    leaving cancel open would only let them cancel someone ELSE's dispatch —
    which is a write to the fleet's work queue by the tier that may not write
    to it.
    """
    _enqueue(_as(fleet["editor"]))
    turn = Turn.objects.get()
    res = _as(fleet["viewer"]).post(f"/api/harness/turns/{turn.id}/cancel")
    assert res.status_code == 403, res.content
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_editor_can_cancel_an_agent_turn(fleet):
    _enqueue(_as(fleet["editor"]))
    turn = Turn.objects.get()
    res = _as(fleet["editor"]).post(f"/api/harness/turns/{turn.id}/cancel")
    assert res.status_code == 200, res.content
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_a_viewer_may_still_cancel_their_own_session_turn(fleet):
    """The carve-out, asserted rather than assumed.

    A session turn is what a chat send produces, and a viewer may chat. So a
    viewer must be able to take back a misfired send — the misfire case the
    phone composer exists for. Gating cancel on the agent's role would take the
    undo away from precisely the tier most likely to need it.
    """
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(
        agent=fleet["agent"], workspace=fleet["ws"], created_by=fleet["viewer"],
        title="chat",
    )
    # `agent` is NULL by construction: turn_targets_agent_xor_project_xor_session
    # allows exactly one target, and a session turn derives its agent (and its
    # tenant) through `chat_session`. That is what makes the carve-out structural
    # rather than a condition the cancel route has to remember to spell out.
    turn = Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        prompt="hello", idempotency_key="sess-1",
    )
    res = _as(fleet["viewer"]).post(f"/api/harness/turns/{turn.id}/cancel")
    assert res.status_code == 200, res.content
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_the_gate_reads_the_one_shared_role_reader(fleet):
    """`wsvc.has_role_at_least` is the single ladder, not a per-module set.

    The harness duplicates the agents surface's resolve helper on purpose (api
    modules must not import each other, and the harness is framework-tier), so
    the thing that must NOT be duplicated is the meaning of "editor". Promoting
    the viewer through the shared service has to change this gate's answer with
    no harness-side edit.
    """
    assert not wsvc.has_role_at_least(fleet["viewer"], WS, WorkspaceMembership.EDITOR)
    wsvc.set_member_role(
        workspace=fleet["ws"], user_id=fleet["viewer"].id, role=WorkspaceMembership.EDITOR
    )
    assert wsvc.has_role_at_least(fleet["viewer"], WS, WorkspaceMembership.EDITOR)
    assert _enqueue(_as(fleet["viewer"]), key="after-promotion").status_code in (200, 201)
