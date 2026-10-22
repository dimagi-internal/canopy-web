"""A sensitive (ZDR) conversation keeps METADATA in canopy-web, never content.

Beyond `test_sensitive_storage.py` (tool results, transcripts, re-exposure): what the
person types, what the agent says and a tool's arguments are stored as metadata only
(kinds, tool names, argument names, ids, sizes, hashes, lifecycle) -- Jonathan,
2026-10-22. The live frame still carries the words to the person watching.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.dispatch import receiver

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, Session
from apps.harness import ledger, sensitivity, services
from apps.harness.models import Turn, TurnEvent, TurnTranscript
from apps.harness.signals import turn_events_appended
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SECRET = "Amina Bello, 3 years, MUAC 11.2 cm"


@pytest.fixture
def ctx():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="wfa", name="Workflow agent", workspace=ws)
    return {"user": jj, "ws": ws, "agent": agent}


def _session(ctx, *, zdr=True):
    return Session.objects.create(agent=ctx["agent"], workspace=ctx["ws"], title="chat",
                                  created_by=ctx["user"],
                                  metadata={"runner_requirements": ["zdr"]} if zdr else {})


def _turn(session, key="t1", prompt=SECRET):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=key, routing=Turn.ANY, prompt=prompt)


EVENTS = [
    {"kind": "assistant", "payload": {"text": f"I looked at {SECRET}"}},
    {"kind": "tool_start", "payload": {"id": "tu_1", "name": "mcp__plugin_canopy_canopy-web__site_call",
                                       "input": {"site": "connect-labs", "tool": "workflow_run_cases",
                                                 "arguments": {"case": SECRET}}}},
    {"kind": "tool_end", "payload": {"tool_use_id": "tu_1", "is_error": False, "content": SECRET}},
]


def test_redaction_keeps_metadata_and_drops_every_word():
    out = sensitivity.redact_payload(EVENTS[1]["payload"])
    assert out["name"] == "mcp__plugin_canopy_canopy-web__site_call" and out["id"] == "tu_1"
    assert out["input_keys"] == ["arguments", "site", "tool"]
    assert out["input_tool"] == "workflow_run_cases" and out["input_site"] == "connect-labs"
    assert out["bytes"] > 0 and len(out["sha256"]) == 16 and out["redacted"] == "zdr"
    assert SECRET not in str(out)
    # Allowlist: an unknown field is dropped, never kept.
    assert "surprise" not in sensitivity.redact_payload({"surprise": "x"})


def test_a_zdr_turns_ledger_stores_metadata_but_streams_the_words(ctx):
    turn = _turn(_session(ctx))
    seen = []

    @receiver(turn_events_appended, dispatch_uid="test_zdr_live")
    def _live(sender, turn, rows, **kwargs):
        seen.extend(r.payload for r in rows)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("django.db.transaction.on_commit", lambda fn, **k: fn())
            ledger.append_events(turn, EVENTS)
    finally:
        turn_events_appended.disconnect(dispatch_uid="test_zdr_live")
    stored = list(TurnEvent.objects.filter(turn=turn).order_by("seq").values_list("payload", flat=True))
    assert len(stored) == 3 and SECRET not in str(stored)
    assert stored[2]["tool_use_id"] == "tu_1" and stored[2]["is_error"] is False
    assert any(SECRET in str(p) for p in seen), "the live fan-out still carries the words"
    # What was projected into the conversation's rows is metadata too.
    rows = list(Message.objects.filter(session=turn.chat_session).values_list("plaintext", "content"))
    assert rows and SECRET not in str(rows)


def test_an_ordinary_turn_is_unchanged(ctx):
    turn = _turn(_session(ctx, zdr=False))
    ledger.append_events(turn, EVENTS)
    assert SECRET in str(list(TurnEvent.objects.filter(turn=turn).values_list("payload", flat=True)))


def test_a_zdr_turn_keeps_no_transcript(ctx):
    turn = _turn(_session(ctx))
    ledger.append_transcript(turn, [f'{{"text": "{SECRET}"}}'])
    t = TurnTranscript.objects.filter(turn=turn).first()
    assert t is None or (t.line_count == 0 and SECRET.encode() not in (ledger.read_transcript(turn) or b""))


def test_the_persons_message_is_kept_as_a_fact_not_its_words(ctx):
    from apps.canopy_sessions import send as chat

    s = _session(ctx)
    message, turn = chat.send_message(session=s, user=ctx["user"], text=SECRET, client_id="c1")
    row = Message.objects.get(pk=message.pk)
    assert row.plaintext == "" and SECRET not in str(row.content)
    assert row.content["redacted"] == "zdr" and row.content["client_id"] == "c1"
    assert turn.prompt == SECRET  # held for the runner, until the turn ends


def test_the_prompt_and_the_note_go_when_the_turn_ends(ctx):
    s = _session(ctx)
    turn = _turn(s)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.DONE, result_note=f"Done: {SECRET}")
    turn.refresh_from_db()
    assert turn.prompt == "" and SECRET not in turn.result_note
    assert turn.result_note.startswith(sensitivity.WITHHELD_PREFIX)


def test_an_ordinary_turn_keeps_its_prompt_and_note(ctx):
    turn = _turn(_session(ctx, zdr=False))
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.DONE, result_note="all good")
    turn.refresh_from_db()
    assert turn.prompt == SECRET and turn.result_note == "all good"


def test_a_turn_started_from_a_zdr_conversation_is_sensitive_too(ctx):
    parent = _turn(_session(ctx))
    child = Turn.objects.create(agent=ctx["agent"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                idempotency_key="child", routing=Turn.ANY, parent_turn=parent, prompt=SECRET)
    assert sensitivity.is_sensitive(child)


def test_no_title_is_written_from_a_zdr_conversation(ctx):
    from apps.canopy_sessions import autotitle

    turn = _turn(_session(ctx))
    called = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(autotitle, "maybe_autotitle", lambda sid: called.append(sid))
        autotitle.on_turn_events(Turn, turn=turn, rows=[TurnEvent(kind="assistant", payload={"text": SECRET})])
    assert called == []


def test_owner_ask_never_copies_words_into_the_note(ctx):
    from apps.harness import owner_ask

    turn = _turn(_session(ctx))
    Turn.objects.filter(pk=turn.pk).update(capability="workflow")
    turn.refresh_from_db()
    assert owner_ask.record(turn, f"For Workflow agent's owner: {SECRET}") is False
