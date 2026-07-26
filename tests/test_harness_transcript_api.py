"""Task 2 (run-convergence, canopy side) — HTTP routes for the raw-transcript
store: POST/GET /api/harness/turns/{turn_id}/transcript.

Reuses `_turn_or_404` for tenancy exactly as every other turn route does — a
transcript is strictly more sensitive than a turn's status, so it gets no
bespoke gate of its own (that's how the session-turn tenancy leak happened).

See .superpowers/sdd/2026-07-26-run-convergence-canopy-side/task-2-brief.md.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def stranger():
    # Email domain outside auto-join so membership stays empty.
    return User.objects.create_user("stranger", "stranger@example.org", "pw")


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


@pytest.fixture()
def owner_client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def stranger_client(stranger):
    c = Client()
    c.force_login(stranger)
    return c


def _enqueue(client, slug="echo", key="k1"):
    resp = client.post(
        "/api/harness/turns/",
        {"agent_slug": slug, "origin": "manual", "idempotency_key": key, "prompt": "/echo:turn"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


def _post_lines(client, turn_id, lines):
    return client.post(
        f"/api/harness/turns/{turn_id}/transcript",
        {"lines": lines},
        content_type="application/json",
    )


def test_append_then_read_back_exactly(owner_client, agent):
    turn_id = _enqueue(owner_client)
    lines = ['{"type": "assistant", "text": "hi"}', '{"type": "result"}']

    resp = _post_lines(owner_client, turn_id, lines)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["line_count"] == 2
    assert body["bytes_raw"] == len("\n".join(lines).encode("utf-8"))

    read = owner_client.get(f"/api/harness/turns/{turn_id}/transcript")
    assert read.status_code == 200
    assert read["Content-Type"] == "application/x-ndjson"
    assert read.content == "\n".join(lines).encode("utf-8")


def test_append_accumulates_across_two_requests(owner_client, agent):
    turn_id = _enqueue(owner_client)
    _post_lines(owner_client, turn_id, ['{"a": 1}'])
    _post_lines(owner_client, turn_id, ['{"a": 2}'])

    read = owner_client.get(f"/api/harness/turns/{turn_id}/transcript")
    assert read.content == b'{"a": 1}\n{"a": 2}'


def test_reading_a_turn_with_no_transcript_is_200_empty_not_404(owner_client, agent):
    turn_id = _enqueue(owner_client)

    resp = owner_client.get(f"/api/harness/turns/{turn_id}/transcript")

    assert resp.status_code == 200
    assert resp.content == b""


def test_unknown_turn_404s_on_both_routes(owner_client, agent):
    missing = uuid.uuid4()
    assert _post_lines(owner_client, missing, ["x"]).status_code == 404
    assert owner_client.get(f"/api/harness/turns/{missing}/transcript").status_code == 404


def test_stranger_gets_404_not_403_on_both_routes(owner_client, stranger_client, agent):
    turn_id = _enqueue(owner_client)
    _post_lines(owner_client, turn_id, ["seed line"])

    assert _post_lines(stranger_client, turn_id, ["x"]).status_code == 404
    assert stranger_client.get(f"/api/harness/turns/{turn_id}/transcript").status_code == 404


def test_stranger_gets_404_on_a_session_turn_transcript(owner, stranger_client, workspace):
    """Regression twin of test_harness_session_turn_tenancy.py: a session turn
    derives tenancy from chat_session.workspace, not from agent — the exact
    fall-through the earlier leak exploited. Both transcript routes must gate
    through the same _turn_or_404 the session-turn fix lives in."""
    session = Session.objects.create(workspace=workspace, created_by=owner, title="t")
    turn = Turn.objects.create(chat_session=session, prompt="hi")

    assert _post_lines(stranger_client, turn.id, ["x"]).status_code == 404
    assert stranger_client.get(f"/api/harness/turns/{turn.id}/transcript").status_code == 404


def test_oversized_batch_is_422(owner_client, agent):
    turn_id = _enqueue(owner_client)
    # Past our 1MB application-level cap, but comfortably under Django's
    # DATA_UPLOAD_MAX_MEMORY_SIZE ceiling (2.5MB default) — so this must
    # reach the view and get our clean 422, not Django's raw-body 500.
    huge = "x" * (int(1.5 * 1024 * 1024))

    resp = _post_lines(owner_client, turn_id, [huge])

    assert resp.status_code == 422
    # Nothing was persisted from the rejected batch.
    read = owner_client.get(f"/api/harness/turns/{turn_id}/transcript")
    assert read.content == b""


def test_append_to_a_terminal_turn_still_succeeds(owner_client, agent):
    """A runner may flush its last batch after finishing — appending to a
    DONE turn must not be blocked."""
    turn_id = _enqueue(owner_client)
    turn = Turn.objects.get(pk=turn_id)
    turn.status = Turn.DONE
    turn.save(update_fields=["status"])

    resp = _post_lines(owner_client, turn_id, ['{"type": "result"}'])

    assert resp.status_code == 200, resp.content
    read = owner_client.get(f"/api/harness/turns/{turn_id}/transcript")
    assert read.content == b'{"type": "result"}'
