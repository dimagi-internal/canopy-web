"""A sensitive conversation's requirements follow its data, and the second lines
behind the caller-token tool limit hold on their own.

The first line is that a sensitive turn's caller token reaches only the four tools
a `data: sensitive` capability may hold. Each test here pins a rule that would
stop a leak the moment that limit loosened: the hard server rule on the token,
lineage that cannot be dropped or shed, a conversation started from a sensitive
one being sensitive itself, one sealing decision for writers and readers, a
refused turn's prompt, forced retention down a chain, and drafts.

Spec: docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §3–§5.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.fixtures.sensitive import vault_key
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.agents.models import Agent
from apps.agents.sensitive_data import SENSITIVE_CANOPY_TOOLS
from apps.canopy_sessions import drafts, transcript_ingest
from apps.canopy_sessions import send as chat_send
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import Draft, Message, RunnerBinding, Session
from apps.common import request_context
from apps.harness import caller_tokens, ledger, provenance, sensitivity, services
from apps.harness import initiator as who
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, Turn
from apps.mcp import turn_scope
from apps.retention import services as retention
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SENSITIVE = {"runner_requirements": ["sensitive_data"]}
ROW = "patient Amina, HIV+, visit 2026-10-01"
#: A capability that says `data: sensitive` but lists more canopy tools than the
#: structural rule allows — what a loosened `structural_refusals` would let through.
LOOSE = {"capabilities": {"ask": {"data": "sensitive", "callers": ["contact", "member"],
                                  "sites": ["labs"], "tools": ["mcp__*canopy-web__*"]}}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()


@pytest.fixture()
def w():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    vault_key(user)  # a sensitive conversation is sealed to its people's vault keys
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=user,
                                 interface=LOOSE)
    hot = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="hot",
                                 metadata=dict(SENSITIVE))
    cold = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="cold")
    raw, _pat = PersonalToken.create_for_user(user=user, label="laptop")
    return {"user": user, "ws": ws, "agent": agent, "hot": hot, "cold": cold, "raw": raw}


def _turn(session=None, **kw):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=uuid.uuid4().hex, **{"prompt": "q", **kw})


def _pat(w) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {w['raw']}")


def _as_token(turn):
    """The request context a caller token of `turn` puts in force."""
    request = RequestFactory().get("/api/me/")
    request.auth_credential = {"type": "caller_token", "id": 1, "label": f"turn:{turn.pk}",
                               "turn_id": str(turn.pk)}
    return request


# --- 1. the hard server rule on a sensitive turn's caller token ---------------------------


def test_a_sensitive_tokens_tools_are_at_most_the_four_whatever_the_interface_lists(w):
    turn = _turn(w["hot"], capability="ask")
    grant = caller_tokens.resolve(caller_tokens.mint(turn))
    assert grant is not None and grant.sensitive and not grant.all_tools
    assert set(grant.tool_globs) == SENSITIVE_CANOPY_TOOLS
    assert not grant.allows_tool("create_turn") and not grant.allows_tool("create_schedule")
    # An ordinary turn of the same capability gets what the interface lists.
    plain = caller_tokens.resolve(caller_tokens.mint(_turn(w["cold"], capability="ask")))
    assert "*" in plain.tool_globs and not plain.sensitive


def test_a_sensitive_token_resolves_to_nothing_once_its_capability_stops_saying_so(w):
    turn = _turn(w["hot"], capability="ask")
    raw = caller_tokens.mint(turn)
    assert caller_tokens.resolve(raw) is not None
    Agent.objects.filter(pk=w["agent"].pk).update(interface={"capabilities": {"ask": {
        **LOOSE["capabilities"]["ask"], "data": None}}})
    assert caller_tokens.resolve(raw) is None


def test_a_sensitive_full_profile_token_is_nothing(w):
    turn = _turn(w["hot"], capability="")
    assert caller_tokens.resolve(caller_tokens.mint(turn, scoped=True)) is None


def test_the_mcp_scope_refuses_anything_but_the_four_for_a_sensitive_session():
    claims = {"sensitive": True, "tool_globs": ["*"], "all_tools": True}
    assert turn_scope._allowed("site_call", claims)
    assert not turn_scope._allowed("create_turn", claims)
    assert not turn_scope._allowed("site_call", {"sensitive": True, "tool_globs": []})


# --- 2. lineage cannot be dropped ---------------------------------------------------------


def test_a_header_cannot_override_a_caller_tokens_turn(w):
    mine, other = _turn(w["hot"]), _turn(w["cold"])
    request = _as_token(mine)
    request.META["HTTP_X_CANOPY_PARENT_TURN"] = str(other.pk)
    assert request_context.from_request(request)["parent"]["turn"] == str(mine.pk)


def test_a_payload_parent_cannot_override_a_caller_tokens_turn(w):
    mine, other = _turn(w["hot"]), _turn(w["cold"])
    with request_context.bound(request_context.from_request(_as_token(mine))):
        child, _ = services.enqueue_turn(
            agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="child",
            initiator=who.for_user(w["user"], via="api", assurance="pat"),
            parent={"turn": str(other.pk)})
    assert child.parent_turn == mine
    assert sensitivity.is_sensitive(child)


def test_an_explicit_parent_that_names_nothing_is_a_422(w):
    nothing = str(uuid.uuid4())
    r = _pat(w).post("/api/harness/turns/", {"agent_slug": "labsdata", "origin": "api",
                                             "idempotency_key": "k", "parent": {"turn": nothing}},
                     content_type="application/json")
    assert r.status_code == 422, r.content
    assert not Turn.objects.filter(idempotency_key="k").exists()
    r = _pat(w).post("/api/canopy-sessions/", {"agent_slug": "labsdata",
                                               "parent": {"session": nothing}},
                     content_type="application/json")
    assert r.status_code == 422, r.content


# --- 3. a conversation started from a sensitive one is sensitive at birth -----------------


def test_a_session_started_from_a_sensitive_turn_or_session_inherits_at_create(w):
    from_turn = chat_services.create_session(workspace=w["ws"], created_by=w["user"],
                                             agent=w["agent"], parent={"turn": _turn(w["hot"])})
    from_session = chat_services.create_session(workspace=w["ws"], created_by=w["user"],
                                                agent=w["agent"], parent={"session": w["hot"]})
    from_cold = chat_services.create_session(workspace=w["ws"], created_by=w["user"],
                                             agent=w["agent"], parent={"session": w["cold"]})
    for s in (from_turn, from_session):
        s.refresh_from_db()
        assert sensitivity.is_sensitive(s) and not s.messages.exists()
    from_cold.refresh_from_db()
    assert not sensitivity.is_sensitive(from_cold)


def test_a_successor_session_carries_its_predecessors_requirements(w):
    runner = Runner.objects.create(name="box", workspace=w["ws"], location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=w["user"])
    binding = RunnerBinding.objects.create(session=w["hot"], runner=runner, session_key="t1")
    with transaction.atomic():
        fresh = transcript_ingest._move_binding_to_successor(w["hot"], binding)
    assert sensitivity.is_sensitive(Session.objects.get(pk=fresh.pk))
    # A successor that already existed (moved back to) is told too.
    with transaction.atomic():
        reused = transcript_ingest._move_binding_to_successor(fresh, binding, successor=w["cold"])
    assert sensitivity.is_sensitive(Session.objects.get(pk=reused.pk))


# --- 4. one sealing decision, for the writer and every reader -----------------------------


def test_a_sensitive_turn_projecting_into_an_ordinary_session_seals_and_reads_sealed(
        w, django_capture_on_commit_callbacks):
    turn = _turn(w["cold"], parent_session=w["hot"])
    assert sensitivity.is_sensitive(turn) and not sensitivity.is_sensitive(w["cold"])
    with django_capture_on_commit_callbacks(execute=True):
        ledger.append_events(turn, [{"kind": "assistant", "payload": {"text": ROW}}])
    w["cold"].refresh_from_db()
    assert sensitivity.is_sensitive(w["cold"])
    row = Message.objects.get(session=w["cold"], role=Message.ASSISTANT)
    assert row.sealed and ROW not in row.plaintext and ROW not in str(row.content)
    # A reader the gate does not admit (MCP, a PAT: not a chat surface) sees metadata.
    out = sensitivity.readable([row], w["cold"], chat_surface=False, user=w["user"])
    assert ROW not in out[0].plaintext


def test_a_turn_enqueued_from_a_sensitive_parent_makes_its_conversation_sensitive(w):
    services.enqueue_turn(session=w["cold"], origin=Turn.ORIGIN_API, idempotency_key="k",
                          initiator=who.for_user(w["user"], via="api", assurance="pat"),
                          parent={"session": w["hot"]})
    w["cold"].refresh_from_db()
    assert sensitivity.is_sensitive(w["cold"])


def test_a_send_whose_turn_is_sensitive_is_sealed_before_it_commits(w):
    message, turn = chat_send.send_message(session=w["cold"], text=ROW, user=w["user"],
                                           client_id="n1", parent={"session": w["hot"]})
    assert turn is not None and sensitivity.is_sensitive(turn)
    row = Message.objects.get(pk=message.pk)
    assert row.sealed and ROW not in row.plaintext and ROW not in str(row.content)
    assert row.content.get("client_id") == "n1"


def test_a_send_decides_on_the_locked_row_not_the_callers_copy(w):
    stale = Session.objects.get(pk=w["cold"].pk)
    chat_services.add_runner_requirements(w["cold"], ["sensitive_data"])
    message, _turn_ = chat_send.send_message(session=stale, text=ROW, user=w["user"],
                                             client_id="n2")
    assert Message.objects.get(pk=message.pk).sealed


# --- 5. a turn refused at creation keeps no prompt -----------------------------------------


def test_a_sensitive_turn_refused_at_creation_keeps_no_prompt(w):
    Agent.objects.filter(pk=w["agent"].pk).update(interface={})
    w["hot"].agent.refresh_from_db()
    contact_turn, _ = services.enqueue_turn(
        session=w["hot"], origin=Turn.ORIGIN_API, idempotency_key="refused", prompt=ROW,
        initiator=who.unknown(via="api"))
    contact_turn.refresh_from_db()
    assert contact_turn.status == Turn.CANCELLED
    assert contact_turn.prompt == ""
    assert contact_turn.result_note.startswith("not run:")


# --- 6. forced retention down a chain; a lost parent fails closed -------------------------


def test_forced_retention_reaches_a_turn_sensitive_only_through_its_parent_turn(w, settings):
    settings.CANOPY_SENSITIVE_KEEP_DAYS = 30
    old = timezone.now() - dt.timedelta(days=31)
    root = _turn(w["hot"], status=Turn.DONE, finished_at=old)
    child = _turn(None, agent=w["agent"], parent_turn=root, status=Turn.DONE, finished_at=old,
                  prompt=ROW)
    plain = _turn(None, agent=w["agent"], parent_turn=_turn(w["cold"]), status=Turn.DONE,
                  finished_at=old, prompt="ordinary")
    retention.sweep(apply=True, sensitive_only=True)
    child.refresh_from_db()
    plain.refresh_from_db()
    assert child.prompt == "" and child.content_purged_at is not None
    assert plain.prompt == "ordinary"


def test_a_queued_turn_whose_parent_was_deleted_is_unclaimable(w):
    parent = _turn(w["hot"])
    child = _turn(None, agent=w["agent"], **provenance.turn_fields(parent={"turn": str(parent.pk)}))
    assert child.status == Turn.QUEUED
    assert child.provenance["parent"]["resolved"] == "turn,session"
    parent.delete()
    w["hot"].delete()
    child = Turn.objects.get(pk=child.pk)
    assert child.parent_turn_id is None
    assert rr.lost_lineage(child)
    assert rr.UNSATISFIABLE in rr.requirements_of(child)
    assert sensitivity.is_sensitive(child)
    # A finished turn is read as it stands.
    Turn.objects.filter(pk=child.pk).update(status=Turn.DONE)
    assert rr.requirements_of(Turn.objects.get(pk=child.pk)) == frozenset()


def test_a_turn_born_without_a_parent_is_not_read_as_having_lost_one(w):
    assert not rr.lost_lineage(_turn(w["cold"]))
    unknown = _turn(w["cold"], provenance={"parent": {"turn": "x", "unresolved": "turn"}})
    assert not rr.lost_lineage(unknown)


# --- 7. drafts ----------------------------------------------------------------------------


def test_a_sensitive_conversations_draft_stays_in_the_browser(w):
    """Nothing a person types into a private conversation is kept until they send it: the
    keystroke path stores nothing there (and the composer sends its text with the send)."""
    assert drafts.update_draft(w["hot"], user=w["user"], expected_version=0, body=ROW) is None
    assert not Draft.objects.filter(session=w["hot"]).exclude(body="").exists()
    assert drafts.commit_draft(w["hot"], w["user"]) == ""
    plain = drafts.update_draft(w["cold"], user=w["user"], expected_version=0, body="hello")
    assert Draft.objects.get(pk=plain.pk).body == "hello"


def test_a_draft_typed_before_it_became_sensitive_is_forgotten_then(w):
    from apps.canopy_sessions import services

    drafts.update_draft(w["cold"], user=w["user"], expected_version=0, body=ROW)
    services.add_runner_requirements(w["cold"], {"sensitive_data"})
    assert not Draft.objects.filter(session=w["cold"]).exclude(body="").exists()


def test_a_draft_stored_under_the_field_key_reads_as_nothing(w):
    """canopy-web does not decrypt one: a body kept that way before reads as empty."""
    from apps.common.encryption import encrypt_secret

    d = drafts.draft_for(w["hot"], w["user"])
    Draft.objects.filter(pk=d.pk).update(body=drafts.SEALED_PREFIX + encrypt_secret(ROW))
    d.refresh_from_db()
    assert drafts.body_of(d) == "" and drafts.commit_draft(w["hot"], w["user"]) == ""


def test_provenance_records_which_parent_keys_resolved(w):
    parent = _turn(w["hot"])
    fields, record = provenance.parent_fields({"turn": str(parent.pk)})
    assert fields["parent_turn"] == parent and record["resolved"] == "turn,session"
