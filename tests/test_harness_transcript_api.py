"""Task 2 (run-convergence, canopy side) — HTTP routes for the raw-transcript
store: POST/GET /api/harness/turns/{turn_id}/transcript.

Reuses `_turn_or_404` for tenancy exactly as every other turn route does — a
transcript is strictly more sensitive than a turn's status, so it gets no
bespoke gate of its own (that's how the session-turn tenancy leak happened).

See .superpowers/sdd/2026-07-26-run-convergence-canopy-side/task-2-brief.md
and the security-review fix round (task-2-report.md) for F1-F8.
"""
from __future__ import annotations

import gzip
import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import services
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


def _post_lines(client, turn_id, lines, batch_id=None, url_prefix=""):
    body = {"lines": lines}
    if batch_id is not None:
        body["batch_id"] = batch_id
    return client.post(
        f"{url_prefix}/api/harness/turns/{turn_id}/transcript",
        body,
        content_type="application/json",
    )


def _get(client, turn_id, url_prefix=""):
    return client.get(f"{url_prefix}/api/harness/turns/{turn_id}/transcript")


def _decoded(response) -> bytes:
    """A real HTTP client transparently inflates `Content-Encoding: gzip` —
    the Django test client does not, so tests do it by hand to simulate what
    the browser/curl --compressed would do."""
    if response.get("Content-Encoding") == "gzip":
        return gzip.decompress(response.content)
    return response.content


def test_append_then_read_back_exactly(owner_client, agent):
    turn_id = _enqueue(owner_client)
    lines = ['{"type": "assistant", "text": "hi"}', '{"type": "result"}']

    resp = _post_lines(owner_client, turn_id, lines)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["line_count"] == 2
    assert body["bytes_raw"] == len("\n".join(lines).encode("utf-8"))
    assert body["truncated"] is False

    read = _get(owner_client, turn_id)
    assert read.status_code == 200
    assert read["Content-Type"] == "application/x-ndjson"
    assert read["Content-Encoding"] == "gzip"  # F3: served compressed, not inflated server-side
    assert _decoded(read) == "\n".join(lines).encode("utf-8")


def test_append_accumulates_across_two_requests(owner_client, agent):
    turn_id = _enqueue(owner_client)
    _post_lines(owner_client, turn_id, ['{"a": 1}'])
    _post_lines(owner_client, turn_id, ['{"a": 2}'])

    read = _get(owner_client, turn_id)
    assert _decoded(read) == b'{"a": 1}\n{"a": 2}'


def test_reading_a_turn_with_no_transcript_is_200_empty_not_404(owner_client, agent):
    turn_id = _enqueue(owner_client)

    resp = _get(owner_client, turn_id)

    assert resp.status_code == 200
    assert resp.content == b""
    # No Content-Encoding on a genuinely empty body — gzip.decompress(b"")
    # raises on the client, and there's nothing to "encode" anyway.
    assert "Content-Encoding" not in resp


def test_unknown_turn_404s_on_both_routes(owner_client, agent):
    missing = uuid.uuid4()
    assert _post_lines(owner_client, missing, ["x"]).status_code == 404
    assert _get(owner_client, missing).status_code == 404


# --- F4: uniform 404 body, no existence/identity leak -----------------------


def test_stranger_gets_404_not_403_on_both_routes_with_a_uniform_body(owner_client, stranger_client, agent):
    turn_id = _enqueue(owner_client)
    _post_lines(owner_client, turn_id, ["seed line"])

    post_resp = _post_lines(stranger_client, turn_id, ["x"])
    get_resp = _get(stranger_client, turn_id)

    assert post_resp.status_code == 404
    assert get_resp.status_code == 404
    for resp in (post_resp, get_resp):
        body = resp.json()
        # Uniform message — never leaks which agent owns the turn.
        assert body["title"] == "turn not found"
        assert body["detail"] == "turn not found"
        assert "echo" not in body["title"].lower()
        assert "echo" not in (body["detail"] or "").lower()


def test_stranger_gets_404_on_a_session_turn_transcript(owner, stranger_client, workspace):
    """Regression twin of test_harness_session_turn_tenancy.py: a session turn
    derives tenancy from chat_session.workspace, not from agent — the exact
    fall-through the earlier leak exploited. Both transcript routes must gate
    through the same _turn_or_404 the session-turn fix lives in."""
    session = Session.objects.create(workspace=workspace, created_by=owner, title="t")
    turn = Turn.objects.create(chat_session=session, prompt="hi")

    assert _post_lines(stranger_client, turn.id, ["x"]).status_code == 404
    assert _get(stranger_client, turn.id).status_code == 404


# --- F1: a workspace-less agent must fail CLOSED, not fall open -------------


def test_null_workspace_agent_transcript_404s_for_any_authenticated_user(owner_client):
    """A pre-tenancy agent with workspace=None used to be silently ungated —
    ANY authenticated user (not just a member of its workspace, since it has
    none) could act on it. For a transcript that would mean handing a
    stranger an agent's full raw `claude -p` output. _agent_or_404 now fails
    closed on a null workspace, so this 404s even for `owner_client`, who
    belongs to a workspace but not to THIS agent's (nonexistent) one."""
    legacy = Agent.objects.create(slug="legacy", name="Legacy")  # no workspace
    turn = Turn.objects.create(agent=legacy, prompt="hi")

    assert _post_lines(owner_client, turn.id, ["x"]).status_code == 404
    assert _get(owner_client, turn.id).status_code == 404


# --- F7: the tenant-prefixed /api/w/{ws}/... form is gated too --------------


def test_tenant_prefixed_form_404s_for_a_member_of_a_different_workspace(owner, agent):
    """The two _turn_or_404 branches that only fire when request.workspace_slug
    is pinned (via /api/w/{ws}/...) were previously unexercised by any
    transcript test. A member of workspace B probing an agent-turn that lives
    in workspace A (`agent`/`workspace` fixtures) via B's own tenant-prefixed
    URL must still 404 — pinned-to-B does not imply visibility into A."""
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="other-ws", display_name="Other", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=other_ws, role=WorkspaceMembership.OWNER)
    other_client = Client()
    other_client.force_login(other)

    owner_client = Client()
    owner_client.force_login(owner)
    turn_id = _enqueue(owner_client)

    prefix = "/api/w/other-ws"
    assert _post_lines(other_client, turn_id, ["x"], url_prefix=prefix).status_code == 404
    assert _get(other_client, turn_id, url_prefix=prefix).status_code == 404


def test_oversized_batch_is_422(owner_client, agent):
    turn_id = _enqueue(owner_client)
    # Past our 1MB application-level cap, but comfortably under Django's
    # DATA_UPLOAD_MAX_MEMORY_SIZE ceiling (2.5MB, pinned in settings) — so
    # this must reach the view and get our clean 422, not a raw-body 500.
    huge = "x" * (int(1.5 * 1024 * 1024))

    resp = _post_lines(owner_client, turn_id, [huge])

    assert resp.status_code == 422
    # Nothing was persisted from the rejected batch.
    read = _get(owner_client, turn_id)
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
    read = _get(owner_client, turn_id)
    assert _decoded(read) == b'{"type": "result"}'


# --- F2: a per-turn ceiling never fails a live turn --------------------------


def test_crossing_the_per_turn_ceiling_still_200s_and_reports_truncated(owner_client, agent, monkeypatch):
    """The whole point of F2: a turn that's still executing must not 4xx just
    because its retained transcript got long. Monkeypatches the real 100MB
    ceiling down to something a single request can cross."""
    monkeypatch.setattr(services, "TRANSCRIPT_TURN_MAX_BYTES", 10)
    turn_id = _enqueue(owner_client)

    resp = _post_lines(owner_client, turn_id, ["this batch is way more than ten bytes"])

    assert resp.status_code == 200, resp.content
    assert resp.json()["truncated"] is True
    read = _get(owner_client, turn_id)
    assert b"way more than ten bytes" not in _decoded(read)


# --- F5: a replayed batch_id is a no-op, not a double-append ----------------


def test_replaying_the_same_batch_id_does_not_double_append(owner_client, agent):
    turn_id = _enqueue(owner_client)
    first = _post_lines(owner_client, turn_id, ["line one"], batch_id="b1")
    assert first.status_code == 200 and first.json()["line_count"] == 1

    # Simulates the runner retrying after the first response was lost.
    retry = _post_lines(owner_client, turn_id, ["line one"], batch_id="b1")

    assert retry.status_code == 200, retry.content
    read = _get(owner_client, turn_id)
    assert _decoded(read) == b"line one"  # not "line one\nline one"


def test_a_new_batch_id_still_appends_normally(owner_client, agent):
    turn_id = _enqueue(owner_client)
    _post_lines(owner_client, turn_id, ["line one"], batch_id="b1")

    _post_lines(owner_client, turn_id, ["line two"], batch_id="b2")

    read = _get(owner_client, turn_id)
    assert _decoded(read) == b"line one\nline two"
