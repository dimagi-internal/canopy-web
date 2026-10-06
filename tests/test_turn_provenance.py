"""Every Turn and Session records WHAT created it and WHY, and says so in the log.

The incident (2026-10-05): a scratch script in one Claude session posted chat
messages to an agent with a person's PAT, and the turns it made were
indistinguishable from that person typing in the web UI — no token id, no
client, no request id, no parent, and no server log of the creation at all.
These tests pin each half of the fix through the real entry points.
"""
from __future__ import annotations

import contextlib
import json
import logging

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.common import request_context
from apps.common.log_format import JsonFormatter
from apps.harness import initiator as who
from apps.harness import provenance, services
from apps.harness.models import AgentSchedule, Turn
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()


@pytest.fixture()
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    workspace = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=workspace,
                                       role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=workspace, owner=owner)
    raw, pat = PersonalToken.create_for_user(user=owner, label="scratch-script")
    return owner, workspace, agent, raw, pat


def _client(raw, **headers) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}", **headers)


def _enqueue(client, key="k1", **body):
    return client.post("/api/harness/turns/",
                       {"agent_slug": "hal", "origin": "api", "idempotency_key": key, **body},
                       content_type="application/json")


@contextlib.contextmanager
def captured(logger_name="canopy.provenance"):
    """Records from a logger that does not propagate (so caplog cannot see it)."""
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger(logger_name)
    handler, level = _H(), lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        yield records
    finally:
        lg.removeHandler(handler)
        lg.setLevel(level)


# --- the credential, the client, the request id -----------------------------------


def test_a_pat_request_records_which_token_and_which_program(ctx):
    _o, _ws, _agent, raw, pat = ctx
    r = _enqueue(_client(raw, HTTP_X_CANOPY_CLIENT="e2e_session_chat.py",
                         HTTP_USER_AGENT="e2e_session_chat.py/1 python-urllib"))
    assert r.status_code == 201, r.content
    turn = Turn.objects.get()
    assert turn.provenance["credential"] == {"type": "pat", "id": pat.pk,
                                             "label": "scratch-script"}
    assert turn.provenance["client"] == "e2e_session_chat.py"
    assert turn.provenance["user_agent"] == "e2e_session_chat.py/1 python-urllib"
    assert turn.provenance["request_id"] == r["X-Request-Id"]
    # Served: the credential and client ride the initiator block too, the ip does not.
    body = r.json()
    assert body["initiator"]["credential"]["id"] == pat.pk
    assert body["initiator"]["client"] == "e2e_session_chat.py"
    assert body["provenance"]["client"] == "e2e_session_chat.py"
    assert "ip" not in body["provenance"]
    assert "ip" in turn.provenance


def test_x_request_id_is_echoed_when_valid_and_minted_otherwise(ctx):
    _o, _ws, _agent, raw, _pat = ctx
    r = _enqueue(_client(raw, HTTP_X_REQUEST_ID="req-abc.123"))
    assert r["X-Request-Id"] == "req-abc.123"
    assert Turn.objects.get().provenance["request_id"] == "req-abc.123"
    r2 = _enqueue(_client(raw, HTTP_X_REQUEST_ID="bad id with spaces\n"), key="k2")
    assert r2["X-Request-Id"] != "bad id with spaces\n"
    assert len(r2["X-Request-Id"]) == 32
    # Every response carries one, not only creations.
    assert Client().get("/health/")["X-Request-Id"]


def test_a_browser_session_is_recorded_as_a_session_credential(ctx):
    owner, *_ = ctx
    c = Client()
    c.force_login(owner)
    assert _enqueue(c).status_code == 201
    assert Turn.objects.get().provenance["credential"]["type"] == "session"


# --- the parent --------------------------------------------------------------------


def _parent_turn(agent, owner):
    session = Session.objects.create(workspace=agent.workspace, agent=agent, created_by=owner)
    turn, _ = services.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                                    idempotency_key="parent",
                                    initiator=who.for_user(owner, via="api", assurance="pat"))
    return turn, session


def test_parent_headers_are_persisted_and_the_session_resolved_from_the_turn(ctx):
    owner, _ws, agent, raw, _pat = ctx
    parent, parent_session = _parent_turn(agent, owner)
    r = _enqueue(_client(raw, HTTP_X_CANOPY_PARENT_TURN=str(parent.pk),
                         HTTP_X_CANOPY_PARENT_TASK="c-scratch-1",
                         HTTP_X_CANOPY_CLAUDE_SESSION="0b1c-claude"))
    assert r.status_code == 201, r.content
    turn = Turn.objects.get(idempotency_key="k1")
    assert turn.parent_turn == parent
    assert turn.parent_session == parent_session
    assert turn.parent_task == "c-scratch-1"
    assert turn.parent_claude_session == "0b1c-claude"
    body = r.json()
    assert body["parent_turn_id"] == str(parent.pk)
    assert body["parent_session_id"] == str(parent_session.pk)
    assert body["initiator"]["parent"]["turn"] == str(parent.pk)


def test_a_host_project_task_parent_resolves_its_session_through_the_binding(ctx):
    owner, _ws, agent, raw, _pat = ctx
    session = Session.objects.create(workspace=agent.workspace, agent=agent, created_by=owner)
    b = RunnerBinding(session=session, session_key="c-scratch-1", host="laptop-1")
    b.emdash_project = "canopy-web"
    b.save()
    r = _enqueue(_client(raw), parent={"task": "c-scratch-1", "host": "laptop-1",
                                       "project": "canopy-web"})
    assert r.status_code == 201, r.content
    turn = Turn.objects.get()
    assert turn.parent_session == session
    assert turn.parent_task == "laptop-1:canopy-web:c-scratch-1"


def test_the_payload_parent_wins_over_the_header(ctx):
    owner, _ws, agent, raw, _pat = ctx
    parent, _s = _parent_turn(agent, owner)
    r = _enqueue(_client(raw, HTTP_X_CANOPY_PARENT_TASK="from-header"),
                 parent={"turn": str(parent.pk), "task": "from-payload"})
    assert r.status_code == 201
    turn = Turn.objects.get(idempotency_key="k1")
    assert (turn.parent_turn, turn.parent_task) == (parent, "from-payload")


def test_an_unknown_parent_is_recorded_raw_and_never_refuses(ctx):
    _o, _ws, _agent, raw, _pat = ctx
    r = _enqueue(_client(raw, HTTP_X_CANOPY_PARENT_TURN="not-a-uuid",
                         HTTP_X_CANOPY_PARENT_SESSION="7d0c3f0e-0000-0000-0000-000000000000"))
    assert r.status_code == 201, r.content
    turn = Turn.objects.get()
    assert turn.parent_turn is None and turn.parent_session is None
    assert turn.provenance["parent"]["turn"] == "not-a-uuid"
    assert turn.provenance["parent"]["unresolved"] == "turn,session"


def test_a_session_created_and_sent_to_records_creator_and_parent(ctx):
    owner, _ws, agent, raw, pat = ctx
    parent, _s = _parent_turn(agent, owner)
    c = _client(raw, HTTP_X_CANOPY_CLIENT="scratch.py", HTTP_X_CANOPY_PARENT_TURN=str(parent.pk))
    r = c.post("/api/canopy-sessions/", {"agent_slug": "hal"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["created_by"] == "jj@dimagi.com"
    assert body["parent_turn_id"] == str(parent.pk)
    assert body["provenance"]["credential"]["id"] == pat.pk
    session = Session.objects.get(pk=body["id"])
    assert session.parent_turn == parent

    r = _client(raw).post(f"/api/canopy-sessions/{session.pk}/send",
                          {"text": "hi", "client_id": "n1", "parent": {"task": "c-other"}},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.parent_task == "c-other"
    assert turn.provenance["credential"]["label"] == "scratch-script"


def test_a_caller_tokens_turn_is_the_parent(ctx):
    """A confined session's caller token names its turn: anything it starts
    is that turn's child, with no header needed."""
    owner, _ws, agent, _raw, _pat = ctx
    parent, _s = _parent_turn(agent, owner)
    request = RequestFactory().get("/api/me/")
    request.auth_credential = {"type": "caller_token", "id": 9, "label": f"turn:{parent.pk}",
                               "turn_id": str(parent.pk)}
    info = request_context.from_request(request)
    assert info["parent"] == {"turn": str(parent.pk)}
    with request_context.bound(info):
        child, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
                                         idempotency_key="child",
                                         initiator=who.for_user(owner, via="api", assurance="pat"))
    assert child.parent_turn == parent
    assert child.provenance["credential"]["type"] == "caller_token"


@contextlib.contextmanager
def _as_claims(**claims):
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"], claims=claims)
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def test_an_mcp_call_records_the_token_the_tool_and_via(ctx):
    owner, _ws, agent, _raw, pat = ctx
    parent, _s = _parent_turn(agent, owner)
    with _as_claims(sub=str(owner.pk), user_id=owner.pk, auth_method="caller_token",
                    turn_id=str(parent.pk), turn_ids=[str(parent.pk)], tool_globs=["*"],
                    token_id=pat.pk, token_label="mcp-laptop"):
        async_to_sync(mcp.call_tool)("enqueue_turn", {
            "agent_slug": "hal", "origin": "api", "idempotency_key": "via-mcp"})
    turn = Turn.objects.get(idempotency_key="via-mcp")
    assert turn.provenance["mcp_tool"] == "enqueue_turn"
    assert turn.provenance["credential"]["id"] == pat.pk
    assert turn.initiator_via == "mcp:enqueue_turn"
    assert turn.parent_turn == parent


# --- the backstop and the internal parents -----------------------------------------


def test_a_raw_create_inside_a_request_is_stamped_anyway(ctx):
    _o, _ws, agent, _raw, _pat = ctx
    with request_context.bound({"client": "canopy-runner", "request_id": "r1"}):
        session = Session.objects.create(workspace=agent.workspace, agent=agent,
                                         origin=Session.ORIGIN_RUNNER)
    assert session.provenance == {"client": "canopy-runner", "request_id": "r1"}


def test_run_now_records_who_clicked(ctx):
    owner, _ws, agent, _raw, _pat = ctx
    schedule = AgentSchedule.objects.create(agent=agent, name="digest", cron="0 9 * * 1",
                                            prompt="/hal:turn", created_by=owner)
    turn = services.run_schedule_now(schedule, clicked_by=owner)
    assert turn.provenance["clicked_by"] == "jj@dimagi.com"


# --- the one log line --------------------------------------------------------------


def test_turn_created_and_session_created_are_logged_on_commit(
        ctx, django_capture_on_commit_callbacks):
    owner, _ws, agent, raw, pat = ctx
    parent, parent_session = _parent_turn(agent, owner)
    with captured() as records, django_capture_on_commit_callbacks(execute=True):
        _client(raw, HTTP_X_CANOPY_CLIENT="scratch.py",
                HTTP_X_CANOPY_PARENT_TURN=str(parent.pk)).post(
            "/api/canopy-sessions/", {"agent_slug": "hal"}, content_type="application/json")
        _enqueue(_client(raw, HTTP_X_CANOPY_CLIENT="scratch.py",
                         HTTP_X_CANOPY_PARENT_TURN=str(parent.pk)),
                 prompt='say "hi"\nthen stop')
    lines = [r.getMessage() for r in records]
    created = [m for m in lines if m.startswith("TURN_CREATED")]
    assert len(created) == 1, lines
    line = created[0]
    assert f"credential=pat:{pat.pk}:scratch-script" in line
    assert "client=scratch.py" in line
    assert f"parent_turn={parent.pk}" in line
    assert f"parent_session={parent_session.pk}" in line
    assert "who=user:jj@dimagi.com" in line
    assert 'prompt_head="say \\"hi\\"\\nthen stop"' in line
    session_lines = [m for m in lines if m.startswith("SESSION_CREATED")]
    assert len(session_lines) == 1 and f"parent_turn={parent.pk}" in session_lines[0]
    rec = next(r for r in records if r.getMessage().startswith("TURN_CREATED"))
    assert rec.prov_credential == f"pat:{pat.pk}:scratch-script"
    assert rec.event == "TURN_CREATED"


def test_a_rolled_back_creation_is_not_logged(ctx, django_capture_on_commit_callbacks):
    from django.db import transaction

    _o, _ws, agent, _raw, _pat = ctx
    with captured() as records, django_capture_on_commit_callbacks(execute=True):
        with contextlib.suppress(RuntimeError), transaction.atomic():
            Session.objects.create(workspace=agent.workspace, agent=agent)
            raise RuntimeError
    assert not records


# --- the JSON log formatter --------------------------------------------------------


def test_json_formatter_emits_parseable_json_with_quotes_and_extras():
    record = logging.LogRecord("canopy.provenance", logging.INFO, __file__, 1,
                               'a "quoted" \\ message\nwith a newline', None, None)
    record.request_id = "rid-1"
    record.prov_credential = "pat:1:x"
    out = json.loads(JsonFormatter().format(record))
    assert out["message"] == 'a "quoted" \\ message\nwith a newline'
    assert out["logger"] == "canopy.provenance"
    assert out["severity"] == "INFO"
    assert out["request_id"] == "rid-1"
    assert out["prov_credential"] == "pat:1:x"
    assert out["timestamp"].endswith("+00:00")


def test_request_id_filter_stamps_records_inside_a_request():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    with request_context.bound({"request_id": "abc"}):
        request_context.RequestIdFilter().filter(record)
    assert record.request_id == "abc"


def test_format_line_quotes_values_with_spaces():
    line = provenance.format_line("TURN_CREATED", {"id": "1", "ua": "a b", "prompt_head": "x"})
    assert line == 'TURN_CREATED id=1 ua="a b" prompt_head="x"'
