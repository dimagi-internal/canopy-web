"""SP2a Task 3 — send_message enqueues a Turn; the ledger projects into Messages."""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services as harness
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    session = chat.create_session(workspace=ws, created_by=user, agent=agent)
    return user, ws, agent, session


def test_send_creates_user_message_and_queued_turn():
    user, _ws, _agent, session = _ctx()
    msg, turn = chat.send_message(session=session, text="hello", user=user)
    assert msg.role == Session.objects.get(pk=session.pk).messages.first().role == "user"
    assert msg.turn_index == 0
    assert msg.plaintext == "hello"
    assert turn.chat_session_id == session.id
    assert turn.status == Turn.QUEUED
    assert turn.prompt == "hello"


def test_distinct_sends_make_distinct_turns():
    user, _ws, _agent, session = _ctx()
    _m, turn1 = chat.send_message(session=session, text="hi", user=user)
    # A second, genuinely different send is a NEW index -> a new turn.
    _m2, turn2 = chat.send_message(session=session, text="again", user=user)
    assert turn1.id != turn2.id


def test_send_with_client_nonce_is_idempotent():
    user, _ws, _agent, session = _ctx()
    m1, turn1 = chat.send_message(session=session, text="hi", user=user, client_id="nonce-1")
    # A retry with the SAME nonce collapses onto the same Message + Turn.
    m2, turn2 = chat.send_message(session=session, text="hi", user=user, client_id="nonce-1")
    assert m1.id == m2.id
    assert turn1.id == turn2.id
    assert session.messages.filter(role="user").count() == 1


def test_projection_materializes_assistant_events():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hello", user=user)
    harness.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi there"}}])
    rows = list(session.messages.order_by("turn_index"))
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[1].plaintext == "hi there"
    assert rows[1].turn_id == turn.id


def test_projection_maps_tool_events_and_is_idempotent():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="do it", user=user)
    harness.append_events(
        turn,
        [
            {"kind": "tool_start", "payload": {"name": "grep"}},
            {"kind": "tool_end", "payload": {"result": "ok"}},
            {"kind": "assistant", "payload": {"text": "done"}},
        ],
    )
    roles = [m.role for m in session.messages.order_by("turn_index")]
    assert roles == ["user", "tool_use", "tool_result", "assistant"]

    # Re-projecting the same ledger rows creates nothing new (idempotent per seq).
    before = session.messages.count()
    chat.project_events(turn, list(turn.events.all()))
    assert session.messages.count() == before


def test_status_events_are_not_transcript_rows():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    harness.append_events(turn, [{"kind": "status", "payload": {"status": "running"}}])
    # Only the user message exists; status is not a transcript row.
    assert [m.role for m in session.messages.all()] == ["user"]


def test_maybe_execute_inline_leaves_turn_for_runner_when_disabled(settings):
    # Production (CHAT_STUB_EXECUTOR=False): a send enqueues and waits for a
    # session-capable cloud runner — no inline stub, no assistant message yet.
    #
    # And NO durable user row: a runner will drive this session in emdash, so the
    # transcript is its record and the user's line lands when the runner ships the
    # record it became (transcript_sourced). Writing one here too would put the
    # same message at two different indexes in one column. Until the turn runs,
    # the text lives in Turn.prompt and the client's optimistic echo.
    settings.CHAT_STUB_EXECUTOR = False
    user, _ws, _agent, session = _ctx()
    msg, turn = chat.send_message(session=session, text="hi", user=user)
    chat.maybe_execute_inline(turn)
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED
    assert session.messages.count() == 0
    assert msg.pk.startswith("transient:")   # serialized to the client, never saved
    assert turn.prompt == "hi"               # the text is not lost while it waits


def test_maybe_execute_inline_runs_stub_when_enabled(settings):
    # Dev/test (default True): the stub runs inline and completes the turn.
    settings.CHAT_STUB_EXECUTOR = True
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    chat.maybe_execute_inline(turn)
    turn.refresh_from_db()
    assert turn.status == Turn.DONE
    assert [m.role for m in session.messages.order_by("turn_index")] == ["user", "assistant"]


# --- Task 9: directed session placement -----------------------------------


def _runner(name: str, *, paired_by) -> Runner:
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=paired_by,
    )


def test_directed_new_chat_pins_first_turn():
    # A session created with a requested runner (a directed new chat) pins its
    # FIRST send there — before any binding exists to make stickiness do the work.
    user = User.objects.create_user("o", "o@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-directed", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    r2 = _runner("r2", paired_by=user)
    session = Session.objects.create(
        workspace=ws, project="canopy-web", metadata={"requested_runner_id": str(r2.id)},
    )
    _msg, turn = chat.send_message(session=session, text="hi", user=user, client_id="c1")
    assert turn.pinned_runner_id == r2.id


def test_send_placement_wait_pins_to_bound_runner():
    # placement="wait" pins the new turn to the session's currently bound runner
    # even though that runner is offline — the point is to hold the turn for it
    # rather than let it drift to whoever else is available.
    user = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-wait", display_name="W", created_by=user)
    r1 = _runner("r1", paired_by=user)  # offline: default Runner.status is DISCONNECTED
    session = Session.objects.create(workspace=ws, project="canopy-web")
    RunnerBinding.objects.create(session=session, runner=r1, thread_key=str(session.id))
    _msg, turn = chat.send_message(
        session=session, text="hi", user=user, client_id="c2", placement="wait",
    )
    assert turn.pinned_runner_id == r1.id


def test_place_repins_queued_turn():
    # The chat banner's after-the-fact decision: place_queued_turn re-pins the
    # oldest still-QUEUED turn without touching the send path.
    user = User.objects.create_user("o3", "o3@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    r1 = _runner("r1", paired_by=user)
    r2 = _runner("r2", paired_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    RunnerBinding.objects.create(session=session, runner=r1, thread_key=str(session.id))
    _msg, turn = chat.send_message(session=session, text="hi", user=user, client_id="c3")
    chat.place_queued_turn(session=session, placement=str(r2.id))
    turn.refresh_from_db()
    assert turn.pinned_runner_id == r2.id


def test_place_wait_requires_bound_runner():
    user = User.objects.create_user("o4", "o4@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place-wait-422", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    chat.send_message(session=session, text="hi", user=user, client_id="c4")
    with pytest.raises(ValueError):
        chat.place_queued_turn(session=session, placement="wait")


def test_place_unknown_runner_raises_value_error():
    user = User.objects.create_user("o5", "o5@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place-unknown", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    chat.send_message(session=session, text="hi", user=user, client_id="c5")
    with pytest.raises(ValueError):
        chat.place_queued_turn(session=session, placement=str(uuid.uuid4()))


def test_place_no_queued_turn_raises_lookup_error():
    user = User.objects.create_user("o6", "o6@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place-empty", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    with pytest.raises(LookupError):
        chat.place_queued_turn(session=session, placement="wait")


def test_send_placement_unknown_runner_raises_value_error():
    user = User.objects.create_user("o7", "o7@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-send-unknown", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    with pytest.raises(ValueError):
        chat.send_message(
            session=session, text="hi", user=user, client_id="c7", placement=str(uuid.uuid4()),
        )


def test_send_placement_foreign_tenant_runner_raises_value_error():
    # A runner paired by a user who is NOT a member of the session's workspace
    # is not a valid placement target — it could never claim the turn it would
    # be pinned to (claim_next_turn's tenant_q derives from paired_by's
    # memberships), so this must 422 like an unknown id, not orphan the turn.
    user = User.objects.create_user("o8", "o8@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-send-foreign", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    outsider = User.objects.create_user("outsider", "outsider@dimagi.com", "pw")
    foreign_runner = _runner("foreign", paired_by=outsider)  # not a member of `ws`
    session = Session.objects.create(workspace=ws, project="canopy-web")

    with pytest.raises(ValueError):
        chat.send_message(
            session=session, text="hi", user=user, client_id="c8",
            placement=str(foreign_runner.id),
        )
    # No turn was ever pinned to the foreign runner.
    assert not Turn.objects.filter(pinned_runner=foreign_runner).exists()


def test_send_placement_malformed_runner_id_raises_value_error():
    # A malformed (non-UUID) placement string must not blow past ValueError into
    # django's ValidationError from the ORM lookup — it 422s exactly like an
    # unknown id (_placeable_runner validates the string before filtering).
    user = User.objects.create_user("o10", "o10@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-send-malformed", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    with pytest.raises(ValueError):
        chat.send_message(
            session=session, text="hi", user=user, client_id="c10", placement="banana",
        )


def test_place_malformed_runner_id_raises_value_error():
    user = User.objects.create_user("o11", "o11@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place-malformed", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, project="canopy-web")
    chat.send_message(session=session, text="hi", user=user, client_id="c11")
    with pytest.raises(ValueError):
        chat.place_queued_turn(session=session, placement="banana")


def test_send_placement_sessions_incapable_runner_raises_value_error():
    # A pin can't route a chat turn to a runner that declares no sessions
    # capability — it would claim the turn (pins bypass target/routing) but
    # could never bridge it. _placeable_runner must reject it like a foreign
    # or unknown runner.
    user = User.objects.create_user("o12", "o12@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-send-incapable", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    incapable = Runner.objects.create(
        name="incapable", kind=Runner.EMDASH, capabilities={}, paired_by=user,
    )
    session = Session.objects.create(workspace=ws, project="canopy-web")
    with pytest.raises(ValueError):
        chat.send_message(
            session=session, text="hi", user=user, client_id="c12",
            placement=str(incapable.id),
        )
    assert not Turn.objects.filter(pinned_runner=incapable).exists()


def test_place_queued_turn_foreign_tenant_runner_raises_value_error():
    # Same gap, after-the-fact: place_queued_turn must reject a runner whose
    # pairer isn't a member of the session's workspace, and must leave the
    # turn's existing pin untouched.
    user = User.objects.create_user("o9", "o9@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-place-foreign", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    outsider = User.objects.create_user("outsider2", "outsider2@dimagi.com", "pw")
    foreign_runner = _runner("foreign2", paired_by=outsider)  # not a member of `ws`

    session = Session.objects.create(workspace=ws, project="canopy-web")
    _msg, turn = chat.send_message(session=session, text="hi", user=user, client_id="c9")
    original_pin = turn.pinned_runner_id

    with pytest.raises(ValueError):
        chat.place_queued_turn(session=session, placement=str(foreign_runner.id))

    turn.refresh_from_db()
    assert turn.pinned_runner_id == original_pin
    assert turn.pinned_runner_id != foreign_runner.id


# ---- origin: the ace_web producer (spec 2026-07-27 source-aware runner routing) ----
# ace-web delegates run execution to canopy (spec 2026-07-26). Until it could say
# so, its turns arrived as `canopy_web_chat` — indistinguishable from a human
# typing in the web UI — so a routing rule on `ace_web` had nothing to match and
# the source-aware routing spec shipped with its ace_web PRODUCER missing.


def test_send_defaults_to_canopy_web_chat_origin():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    assert turn.origin == Turn.ORIGIN_CANOPY_WEB_CHAT


def test_send_carries_an_explicit_origin():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user, origin=Turn.ORIGIN_ACE_WEB)
    assert turn.origin == Turn.ORIGIN_ACE_WEB


def test_transcript_sourced_send_carries_the_origin_too():
    # The path labs actually takes: every session is stamped transcript_sourced at
    # creation, so a run dispatched by ace-web lands in _send_transcript_sourced_
    # message. Threading the origin through only the durable path would leave
    # production exactly as broken as before.
    user, _ws, _agent, session = _ctx()
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    assert chat.transcript_sourced(session)
    _msg, turn = chat.send_message(session=session, text="hi", user=user, origin=Turn.ORIGIN_ACE_WEB)
    assert turn.origin == Turn.ORIGIN_ACE_WEB


# ---- origin derived from the session's owning product ----
# #496 threaded origin only through ace-web's SERVER-side run dispatcher, so a
# human typing into ace-web's OWN chat produced canopy_web_chat and routed like
# canopy chat. Observed live: "testing" typed into an ace-web session went to a
# laptop runner instead of the box that runs ace-web's work. The distinction
# that matters is which PRODUCT the session belongs to, not who typed.


def _ace_web_session(**meta):
    user = User.objects.create_user("aw", "aw@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="aw-ws", display_name="W", created_by=user)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=user)
    session = chat.create_session(workspace=ws, created_by=user, agent=agent)
    session.metadata = {**(session.metadata or {}), "source": "ace-web", **meta}
    session.save(update_fields=["metadata"])
    return user, session


def test_a_human_typing_in_an_ace_web_session_is_ace_web_work():
    user, session = _ace_web_session(origin_key="ace-web:dimagi-team")
    _msg, turn = chat.send_message(session=session, text="testing", user=user)
    assert turn.origin == Turn.ORIGIN_ACE_WEB


def test_a_canopy_session_is_still_chat():
    user, _ws, _agent, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    assert turn.origin == Turn.ORIGIN_CANOPY_WEB_CHAT


def test_transcript_sourced_ace_web_session_also_derives_ace_web():
    # The path labs actually takes — every session is transcript_sourced at
    # creation, so deriving on the durable path only would be a no-op in prod.
    user, session = _ace_web_session()
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    assert chat.transcript_sourced(session)
    _msg, turn = chat.send_message(session=session, text="testing", user=user)
    assert turn.origin == Turn.ORIGIN_ACE_WEB


def test_an_explicit_origin_still_wins():
    # The run dispatcher names ace_web outright; derivation must not fight it,
    # and a caller naming something else must not be silently overridden.
    user, session = _ace_web_session()
    _msg, turn = chat.send_message(session=session, text="x", user=user,
                                   origin=Turn.ORIGIN_EMAIL)
    assert turn.origin == Turn.ORIGIN_EMAIL


def test_unknown_or_missing_source_falls_back_to_chat():
    user, _ws, _agent, session = _ctx()
    for meta in ({}, {"source": "something-else"}, {"source": None}):
        session.metadata = meta
        assert chat.default_origin(session) == Turn.ORIGIN_CANOPY_WEB_CHAT, meta
