"""A caller token's reach holds for a turn's CONTENT, not only for sessions.

`turn_access.can_read_turn_log` asked the session ACL only for a turn with `chat_session_id`.
Any other turn fell through to the initiator, box-owner, `LOGS_READ` and agent-admin legs, so an
ordinary agent turn's caller token, asked by someone holding one of those, read turns outside
the reach. Through real tokens, on REST and the mounted MCP server.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent, AgentGrant
from apps.canopy_sessions.models import Session
from apps.harness import claim_payload, initiator, services
from apps.harness.models import Runner, Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db
SECRET = "the words of someone else's turn"
#: Every door that reads a turn's content.
READS = ("read_turn_events", "read_turn_messages")


@pytest.fixture
def w():
    ws = a_workspace()
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    ana = User.objects.create_user("ana", "ana@dimagi.com", "pw")     # a workspace admin: LOGS_READ
    wsvc.ensure_member(ws, ana, WorkspaceMembership.ADMIN)
    bo = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    wsvc.ensure_member(ws, bo, WorkspaceMembership.EDITOR)
    agents = {slug: Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
              for slug in ("trellis", "hal")}
    for a in agents.values():
        Agent.objects.filter(pk=a.pk).update(inherit_workspace_defaults=False)
        AgentGrant.objects.create(agent=a, kind=AgentGrant.PERSON, user=ana, role="editor")
    box = Runner.objects.create(name="ana-box", kind=Runner.CLOUD, owner=ana, workspace=ws,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    return {"ws": ws, "owner": owner, "ana": ana, "bo": bo, "box": box, **agents}


def _turn(*, by, agent=None, session=None, status=Turn.DONE, **kw):
    target = {"chat_session": session} if session is not None else {"agent": agent}
    return Turn.objects.create(**target, origin=Turn.ORIGIN_API, prompt=SECRET, status=status,
                               idempotency_key=uuid.uuid4().hex, initiator_kind=initiator.USER,
                               initiator_via="api", initiator_assurance="session", initiator_user=by, **kw)


def _token(w):
    """ana's ordinary Trellis turn, running, and the scoped caller token it really gets."""
    t, _ = services.enqueue_turn(agent=w["trellis"], origin=Turn.ORIGIN_API, prompt=SECRET,
                                 idempotency_key=uuid.uuid4().hex,
                                 initiator=initiator.for_user(w["ana"], via="chat", assurance="session"))
    Turn.objects.filter(pk=t.pk).update(status=Turn.RUNNING, claimed_by=w["box"])
    t.refresh_from_db()
    raw = claim_payload.issue_credentials(t).mcp_token
    assert raw and not t.capability
    return t, raw


@contextlib.contextmanager
def as_token(raw):
    token = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert token is not None
    tok = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _rest(raw, path):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {raw}")


def _rest_reads(raw, turn) -> bool:
    """Whether REST hands this token the turn's content, by every door."""
    base = f"/api/harness/turns/{turn.pk}"
    got = [_rest(raw, f"{base}/events").status_code == 200,
           _rest(raw, f"{base}/messages").status_code == 200,
           _rest(raw, f"{base}/caller-context").status_code == 200,
           SECRET in _rest(raw, base).content.decode()]
    assert len(set(got)) == 1, got
    return got[0]


def _mcp_reads(raw, turn) -> bool:
    outcomes = []
    with as_token(raw):
        for name in READS:
            try:
                async_to_sync(mcp.call_tool)(name, {"turn_id": str(turn.pk)})
                outcomes.append(True)
            except ToolError:
                outcomes.append(False)
    assert len(set(outcomes)) == 1, outcomes
    return outcomes[0]


def test_another_agents_turn_is_out_of_reach_even_for_someone_with_logs_read(w):
    _t, raw = _token(w)
    theirs = _turn(by=w["bo"], agent=w["hal"])
    assert not _rest_reads(raw, theirs)
    assert not _mcp_reads(raw, theirs)


def test_their_own_turn_with_another_agent_is_out_of_reach(w):
    _t, raw = _token(w)
    own = _turn(by=w["ana"], agent=w["hal"])
    assert not _rest_reads(raw, own)
    assert not _mcp_reads(raw, own)


def test_a_turn_on_a_box_they_own_with_another_agent_is_out_of_reach(w):
    _t, raw = _token(w)
    on_box = _turn(by=w["bo"], agent=w["hal"], claimed_by=w["box"])
    assert not _rest_reads(raw, on_box)
    assert not _mcp_reads(raw, on_box)


def test_a_turn_naming_its_chat_only_in_origin_ref_is_judged_by_that_chat(w):
    _t, raw = _token(w)
    their_chat = Session.objects.create(workspace=w["ws"], agent=w["hal"], created_by=w["bo"], title="private")
    named = _turn(by=w["bo"], agent=w["trellis"], origin_ref={"chat_session_id": str(their_chat.pk)})
    assert not _rest_reads(raw, named)
    assert not _mcp_reads(raw, named)


def test_a_named_chat_that_does_not_exist_reads_as_no_chat_and_stays_inside_the_agent(w):
    """A turn of this agent with no real conversation is judged as the agent's, by the old legs."""
    _t, raw = _token(w)
    named = _turn(by=w["ana"], agent=w["trellis"], origin_ref={"chat_session_id": str(uuid.uuid4())})
    assert _rest_reads(raw, named) is True


def test_the_agents_own_turns_stay_in_reach(w):
    t, raw = _token(w)
    assert _rest_reads(raw, t) is True
    assert _mcp_reads(raw, t) is True
    another_of_its = _turn(by=w["bo"], agent=w["trellis"])
    assert _rest_reads(raw, another_of_its) is True    # ana has LOGS_READ, and it is this agent's


def test_a_chat_turn_with_this_agent_that_they_cannot_read_stays_closed(w):
    _t, raw = _token(w)
    chat = Session.objects.create(workspace=w["ws"], agent=w["trellis"], created_by=w["bo"], title="bo's")
    assert not _rest_reads(raw, _turn(by=w["bo"], session=chat))


def test_the_lists_redact_what_is_out_of_reach(w):
    _t, raw = _token(w)
    theirs = _turn(by=w["bo"], agent=w["hal"])
    rows = _rest(raw, "/api/harness/turns/?limit=50").json()
    row = next((r for r in rows if r["id"] == str(theirs.pk)), None)
    assert row is None or row["prompt"] == ""
    assert SECRET not in _rest(raw, f"/api/agents/{w['hal'].slug}/turns/").content.decode()


def test_a_personal_projects_reader_is_still_held_to_the_reach(w, monkeypatch):
    """A personal project's rule answers for itself, so the reach is asked before it."""
    from apps.agents import services as agent_services  # noqa: PLC0415 — this test alone
    from apps.harness import turn_access  # noqa: PLC0415 — this test alone

    monkeypatch.setattr(turn_access, "_personal_project", lambda turn, memo: object())
    monkeypatch.setattr(agent_services, "may_read_project", lambda user, project: True)
    _t, raw = _token(w)
    assert not _rest_reads(raw, _turn(by=w["ana"], agent=w["hal"]))           # out of reach: no
    assert _rest_reads(raw, _turn(by=w["ana"], agent=w["trellis"])) is True    # in reach: its rule


def test_the_session_doors_hold_to_the_reach_too(w):
    """The sweep of what serves a session's words: each door asks the one ACL, so each stops here."""
    _t, raw = _token(w)
    out = Session.objects.create(workspace=w["ws"], created_by=w["ana"], title="her laptop-like chat")
    base = f"/api/canopy-sessions/{out.pk}"
    for path in (base, f"{base}/messages?before=9", f"{base}/human-inputs", f"{base}/export",
                 f"{base}/attachments", f"{base}/sealed-attachments", f"{base}/handoffs"):
        assert _rest(raw, path).status_code in (403, 404, 405), path
    for path in ("/api/me/sessions/", "/api/harness/sessions/", "/api/canopy-sessions/search"):
        assert str(out.pk) not in _rest(raw, path).content.decode(), path


def test_nobody_held_to_no_reach_loses_anything(w):
    """A person's own sign-in and PAT, and the agent admin's: unchanged."""
    theirs = _turn(by=w["bo"], agent=w["hal"])
    ana = Client()
    ana.force_login(w["ana"])
    assert ana.get(f"/api/harness/turns/{theirs.pk}/events").status_code == 200
    pat, _ = PersonalToken.create_for_user(user=w["ana"], label="mine")
    assert _rest(pat, f"/api/harness/turns/{theirs.pk}/events").status_code == 200
