"""What created a turn or a session, and why — recorded and logged once.

The incident: a scratch script in one Claude session posted chat messages to
`hal` with Jonathan's PAT, and canopy-web could not tell it from the web UI.
These drive the real entry points (the REST routes, through the middleware
stack) and assert the row, the API and the one creation log line all say which
token, which program, which request and which parent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client, RequestFactory

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.common import request_context
from apps.common.log_format import JsonFormatter
from apps.harness import provenance, services
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

UA = "canopy-cli/0.2.590 (python/3.12; darwin)"


@pytest.fixture()
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    workspace = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=workspace,
                                       role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=workspace, owner=owner)
    raw, token = PersonalToken.create_for_user(user=owner, label="scratch script")
    return owner, workspace, agent, raw, token


def _client(raw: str, **headers) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_USER_AGENT=UA,
                  HTTP_X_CANOPY_CLIENT="canopy-cli/0.2.590", **headers)


def _turn(client, key="k1", **extra):
    r = client.post("/api/harness/turns/",
                    {"agent_slug": "hal", "origin": "api", "idempotency_key": key, **extra},
                    content_type="application/json")
    assert r.status_code == 201, r.content
    return r


def test_a_pat_turn_records_which_token_and_which_program(ctx):
    _owner, _ws, _agent, raw, token = ctx
    r = _turn(_client(raw))

    turn = Turn.objects.get()
    assert turn.provenance["credential"] == {"type": "pat", "id": token.pk,
                                             "label": "scratch script"}
    assert turn.provenance["client"] == "canopy-cli/0.2.590"
    assert turn.provenance["user_agent"] == UA
    assert turn.provenance["request_id"] == r["X-Request-Id"]
    # …and the API says so, with the credential on the initiator too.
    body = r.json()
    assert body["provenance"]["credential"]["id"] == token.pk
    assert body["initiator"]["credential"] == {"type": "pat", "id": token.pk,
                                               "label": "scratch script"}
    assert body["initiator"]["client"] == "canopy-cli/0.2.590"


def test_a_browser_turn_records_a_session_credential(ctx):
    owner, *_ = ctx
    c = Client()
    c.force_login(owner)
    r = c.post("/api/harness/turns/", {"agent_slug": "hal", "origin": "api",
                                       "idempotency_key": "k"},
               content_type="application/json")
    assert r.status_code == 201, r.content
    assert Turn.objects.get().provenance["credential"]["type"] == "session"


def test_x_request_id_is_echoed_and_minted(ctx):
    *_, raw, _token = ctx
    r = _client(raw, HTTP_X_REQUEST_ID="abc-123").get("/api/me/")
    assert r["X-Request-Id"] == "abc-123"
    minted = _client(raw).get("/api/me/")["X-Request-Id"]
    assert minted and minted != "abc-123"
    # A malformed id is replaced, not echoed into a log line.
    assert _client(raw, HTTP_X_REQUEST_ID="bad id\n").get("/api/me/")["X-Request-Id"] != "bad id\n"


def test_parent_headers_are_persisted_and_resolved(ctx):
    owner, ws, agent, raw, _token = ctx
    parent_session = Session.objects.create(workspace=ws, agent=agent, created_by=owner)
    parent_turn, _ = services.enqueue_turn(session=parent_session, origin="api",
                                           idempotency_key="parent")
    _turn(_client(raw, HTTP_X_CANOPY_PARENT_TURN=str(parent_turn.pk),
                  HTTP_X_CANOPY_PARENT_TASK="c-scratch",
                  HTTP_X_CANOPY_PARENT_HOST="jj-mbp",
                  HTTP_X_CANOPY_CLAUDE_SESSION="11111111-2222-3333-4444-555555555555"))

    turn = Turn.objects.get(idempotency_key="k1")
    assert turn.parent_turn_id == parent_turn.pk
    # Only the turn was named: its own conversation is the parent session.
    assert turn.parent_session_id == parent_session.pk
    assert turn.parent_task == "c-scratch"
    assert turn.parent_claude_session == "11111111-2222-3333-4444-555555555555"
    assert turn.provenance["parent"]["host"] == "jj-mbp"


def test_a_parent_task_resolves_to_its_session_through_the_binding(ctx):
    owner, ws, agent, raw, _token = ctx
    laptop = Session.objects.create(workspace=ws, agent=agent, created_by=owner)
    RunnerBinding.objects.create(session=laptop, session_key="c-scratch", host="jj-mbp")
    _turn(_client(raw), parent={"task": "c-scratch", "host": "jj-mbp"})
    assert Turn.objects.get().parent_session_id == laptop.pk


def test_an_unknown_parent_is_recorded_not_refused(ctx):
    *_, raw, _token = ctx
    ghost = str(uuid.uuid4())
    _turn(_client(raw, HTTP_X_CANOPY_PARENT_TURN="not-a-uuid"),
          parent={"session_id": ghost})
    turn = Turn.objects.get()
    assert turn.parent_turn_id is None and turn.parent_session_id is None
    assert turn.provenance["parent_unresolved"] == {"turn_id": "not-a-uuid",
                                                    "session_id": ghost}


def test_a_payload_parent_wins_over_the_header(ctx):
    owner, ws, agent, raw, _token = ctx
    a, _ = services.enqueue_turn(agent=agent, origin="api", idempotency_key="a")
    b, _ = services.enqueue_turn(agent=agent, origin="api", idempotency_key="b")
    _turn(_client(raw, HTTP_X_CANOPY_PARENT_TURN=str(a.pk)), parent={"turn_id": str(b.pk)})
    assert Turn.objects.get(idempotency_key="k1").parent_turn_id == b.pk


def test_session_create_and_send_record_provenance_and_parent(ctx):
    owner, ws, agent, raw, token = ctx
    parent = Session.objects.create(workspace=ws, agent=agent, created_by=owner)
    c = _client(raw, HTTP_X_CANOPY_PARENT_SESSION=str(parent.pk))
    r = c.post("/api/canopy-sessions/", {"agent_slug": "hal"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["created_by"] == "jj@dimagi.com"
    assert body["parent_session_id"] == str(parent.pk)
    assert body["provenance"]["credential"]["id"] == token.pk

    sid = body["id"]
    r = c.post(f"/api/canopy-sessions/{sid}/send", {"text": "hi", "client_id": "n1"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.provenance["client"] == "canopy-cli/0.2.590"
    assert turn.parent_session_id == parent.pk


def test_a_caller_token_auto_fills_the_parent_turn(ctx):
    """A confined session's caller token names the turn it was minted for —
    the parent of anything that session creates, with no header needed."""
    _owner, _ws, agent, _raw, _token = ctx
    origin, _ = services.enqueue_turn(agent=agent, origin="api", idempotency_key="origin")
    request = RequestFactory().get("/api/me/")
    request.META["HTTP_USER_AGENT"] = "httpx"
    request.auth_credential = {"type": "caller_token", "id": str(origin.pk),
                               "label": "", "turn_id": str(origin.pk)}
    request.via_mcp = True
    request.mcp_tool = "create_turn"
    request.mcp_outer = {"user_agent": "claude-code/2.1", "client": ""}
    built = request_context.build(request)
    assert built["parent"]["turn_id"] == str(origin.pk)
    assert built["client"] == "mcp" and built["user_agent"] == "claude-code/2.1"

    token = request_context.set_current(built)
    try:
        child, _ = services.enqueue_turn(agent=agent, origin="api", idempotency_key="child")
    finally:
        request_context.reset(token)
    assert child.parent_turn_id == origin.pk
    assert child.provenance["mcp_tool"] == "create_turn"


@pytest.mark.django_db(transaction=True)
def test_mcp_pat_claims_carry_the_token(ctx):
    *_, raw, token = ctx
    from apps.mcp.auth import CanopyPATVerifier

    access = asyncio.run(CanopyPATVerifier().verify_token(raw))
    assert access.claims["token_id"] == token.pk
    assert access.claims["credential"]["label"] == "scratch script"


def test_turn_and_session_created_are_logged_once_with_credential_and_parent(
        ctx, caplog, django_capture_on_commit_callbacks):
    owner, ws, agent, raw, token = ctx
    parent = Session.objects.create(workspace=ws, agent=agent, created_by=owner)
    c = _client(raw, HTTP_X_CANOPY_PARENT_SESSION=str(parent.pk))
    with caplog.at_level(logging.INFO, logger="canopy.provenance"), \
            django_capture_on_commit_callbacks(execute=True):
        sid = c.post("/api/canopy-sessions/", {"agent_slug": "hal"},
                     content_type="application/json").json()["id"]
        c.post(f"/api/canopy-sessions/{sid}/send",
               {"text": 'say "hi"\nplease', "client_id": "n1"},
               content_type="application/json")
    lines = [r for r in caplog.records if r.name == "canopy.provenance"]
    created = [r.getMessage() for r in lines]
    sessions = [m for m in created if m.startswith("SESSION_CREATED")]
    turns = [m for m in created if m.startswith("TURN_CREATED")]
    assert len(sessions) == 1 and len(turns) == 1, created
    cred = f'credential="pat:{token.pk}:scratch script"'
    assert cred in sessions[0] and f"parent_session={parent.pk}" in sessions[0]
    assert cred in turns[0] and "who=user:jj@dimagi.com" in turns[0]
    assert f"session={sid}" in turns[0] and "client=canopy-cli/0.2.590" in turns[0]
    assert 'prompt_head="say \\"hi\\"\\nplease"' in turns[0]
    turn_record = next(r for r in lines if r.getMessage().startswith("TURN_CREATED"))
    assert turn_record.event == "TURN_CREATED"
    assert turn_record.parent_session == str(parent.pk)


def test_a_replayed_enqueue_does_not_log_twice(ctx, caplog, django_capture_on_commit_callbacks):
    *_, agent, _raw, _token = ctx
    with caplog.at_level(logging.INFO, logger="canopy.provenance"), \
            django_capture_on_commit_callbacks(execute=True):
        services.enqueue_turn(agent=agent, origin="api", idempotency_key="same")
        services.enqueue_turn(agent=agent, origin="api", idempotency_key="same")
    assert sum(r.getMessage().startswith("TURN_CREATED") for r in caplog.records) == 1


def test_schedule_run_now_records_who_clicked(ctx):
    owner, _ws, agent, _raw, _token = ctx
    from apps.harness.models import AgentSchedule

    schedule = AgentSchedule.objects.create(agent=agent, name="weekly", cron="0 9 * * 1",
                                            prompt="/hal:turn", created_by=owner)
    turn = services.run_schedule_now(schedule, clicked_by=owner)
    assert turn.provenance["clicked_by"] == {"id": owner.pk, "email": "jj@dimagi.com"}


def test_the_json_formatter_emits_parseable_json_with_quotes_and_fields():
    record = logging.LogRecord("canopy.provenance", logging.INFO, __file__, 1,
                               'TURN_CREATED prompt_head="a \\"quoted\\" line"\nnext', None, None)
    record.request_id = "rid-1"
    record.parent_turn = "t-1"
    out = json.loads(JsonFormatter().format(record))
    assert out["message"].startswith("TURN_CREATED") and "\n" in out["message"]
    assert out["logger"] == "canopy.provenance" and out["severity"] == "INFO"
    assert out["request_id"] == "rid-1" and out["parent_turn"] == "t-1"
    assert out["timestamp"]


def test_the_request_id_filter_stamps_records_inside_a_request():
    token = request_context.set_current({"request_id": "rid-9"})
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        request_context.RequestIdLogFilter().filter(record)
    finally:
        request_context.reset(token)
    assert record.request_id == "rid-9"


def test_format_line_quotes_free_text():
    line = provenance.format_line("TURN_CREATED", {"id": "1", "ua": "a b", "client": "x y",
                                                   "prompt_head": 'he said "no"'})
    assert line == ('TURN_CREATED id=1 ua="a b" client="x y" prompt_head="he said \\"no\\""')
