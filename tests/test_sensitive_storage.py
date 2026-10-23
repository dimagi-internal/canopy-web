"""A sensitive conversation (it requires ZDR runners) keeps the host's data inside
the boundary: canopy stores less, purges on its own clock, and never re-exposes or
re-processes it. One test per surface.

Spec: docs/superpowers/specs/2026-10-22-phi-agents-on-zdr-runners-design.md §4.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.events import usage
from apps.events.models import SessionUsage
from apps.harness import ledger, sensitivity
from apps.harness.models import Runner, Turn, TurnEvent, TurnTranscript
from apps.retention import services as retention
from apps.retention.models import RetentionSweep
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ZDR = {"runner_requirements": ["zdr"]}
ROW = "patient Amina, HIV+, visit 2026-10-01"


@pytest.fixture()
def w():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=user)
    runner = Runner.objects.create(name="box", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user)
    hot = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="hot",
                                 metadata=dict(ZDR))
    cold = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="cold")
    c = Client()
    c.force_login(user)
    return {"user": user, "ws": ws, "agent": agent, "runner": runner, "hot": hot, "cold": cold,
            "c": c}


def _turn(session, **kw):
    return Turn.objects.create(chat_session=session, prompt="q", origin=Turn.ORIGIN_API,
                               idempotency_key=uuid.uuid4().hex, **kw)


# --- the predicate ----------------------------------------------------------------------


def test_the_predicate_reads_requirements_through_sessions_and_turns(w):
    assert sensitivity.is_sensitive(w["hot"]) and not sensitivity.is_sensitive(w["cold"])
    assert sensitivity.is_sensitive(_turn(w["hot"]))
    assert not sensitivity.is_sensitive(_turn(w["cold"]))
    # A child turn of a sensitive conversation, with no session of its own.
    child = Turn.objects.create(agent=w["agent"], prompt="q", origin=Turn.ORIGIN_API,
                                idempotency_key="child", parent_session=w["hot"])
    assert sensitivity.is_sensitive(child)
    # A requirement canopy cannot read counts.
    w["cold"].metadata = {"runner_requirements": "garbage"}
    assert sensitivity.is_sensitive(w["cold"])
    assert not sensitivity.is_sensitive(None)


# --- store less -------------------------------------------------------------------------


def test_no_transcript_is_kept(w):
    t = _turn(w["hot"])
    out = ledger.append_transcript(t, ['{"type":"user","text":"' + ROW + '"}'])
    assert out.line_count == 0 and out.pk is None
    assert not TurnTranscript.objects.filter(turn=t).exists()
    ledger.append_transcript(cold := _turn(w["cold"]), ['{"a":1}'])
    assert TurnTranscript.objects.filter(turn=cold).exists()


def test_the_runner_still_gets_success_for_a_transcript_batch(w):
    t = _turn(w["hot"], claimed_by=w["runner"], status=Turn.RUNNING)
    with mock.patch("apps.harness.api.reporting_turn_or_404", return_value=t):
        r = w["c"].post(f"/api/harness/turns/{t.pk}/transcript",
                        data={"lines": ['{"x":"' + ROW + '"}']}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["line_count"] == 0
    assert not TurnTranscript.objects.exists()


def test_a_tool_end_event_keeps_the_call_and_status_never_the_result(
        w, django_capture_on_commit_callbacks):
    t = _turn(w["hot"])
    with django_capture_on_commit_callbacks(execute=True):
        ledger.append_events(t, [
            {"kind": "tool_start", "payload": {"id": "tu1", "name": "mcp__canopy-web__site_call",
                                               "input": {"tool": "data_query"}}},
            {"kind": "tool_end", "payload": {"tool_use_id": "tu1", "is_error": False,
                                             "content": [{"type": "text", "text": ROW}]}},
        ])
    start, end = TurnEvent.objects.filter(turn=t).order_by("seq")
    # Metadata only (Jonathan, 2026-10-22): which tool, the names of its arguments and
    # the host tool reached are kept; the arguments are not.
    assert "input" not in start.payload
    assert start.payload["name"] == "mcp__canopy-web__site_call" and start.payload["input_tool"] == "data_query"
    assert {k: end.payload[k] for k in ("tool_use_id", "is_error", "withheld")} == {
        "tool_use_id": "tu1", "is_error": False, "withheld": True}
    assert ROW not in str(end.payload)
    # ...and so is the TOOL_RESULT message projected from it.
    projected = Message.objects.get(session=w["hot"], role=Message.TOOL_RESULT)
    assert ROW not in str(projected.content) and ROW not in projected.plaintext
    cold = _turn(w["cold"])
    ledger.append_events(cold, [{"kind": "tool_end", "payload": {"tool_use_id": "x", "content": ROW}}])
    assert TurnEvent.objects.get(turn=cold).payload["content"] == ROW


def test_a_runner_sessions_tool_result_row_keeps_no_result(w):
    n = chat_transcript_ingest.persist_transcript_rows(w["hot"], [
        {"index": 0, "role": "user", "text": "how many visits?"},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "tu1", "name": "site_call", "input": {"tool": "data_query"}}},
        {"index": 65, "role": "tool_result", "text": ROW,
         "content": {"tool_use_id": "tu1", "is_error": False, "text": ROW}},
        {"index": 128, "role": "assistant", "text": "12 visits"},
    ])
    assert n == 4
    rows = {m.role: m for m in Message.objects.filter(session=w["hot"])}
    assert rows[Message.TOOL_RESULT].plaintext == ""
    assert rows[Message.TOOL_RESULT].content == {"tool_use_id": "tu1", "is_error": False,
                                                 "withheld": True}
    # Metadata only (Jonathan, 2026-10-22): the rows record that each was said, never
    # the words or a tool's arguments.
    assert "input" not in rows[Message.TOOL_USE].content
    assert rows[Message.TOOL_USE].content["input_tool"] == "data_query"
    assert rows[Message.ASSISTANT].plaintext == "" and "12 visits" not in str(rows[Message.ASSISTANT].content)
    assert rows[Message.USER].plaintext == "" and "how many" not in str(rows[Message.USER].content)


def test_the_live_frame_carries_the_stripped_row(w, monkeypatch):
    w["hot"].origin = Session.ORIGIN_RUNNER
    w["hot"].save(update_fields=["origin"])
    RunnerBinding.objects.create(session=w["hot"], runner=w["runner"], session_key="k",
                                 stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    r = w["c"].post(f"/api/harness/runners/{w['runner'].id}/session-stream", data={
        "session_id": str(w["hot"].id), "transcript_id": "t-1",
        "events": [{"kind": "tool_result", "seq": 1, "index": 65,
                    "payload": {"tool_use_id": "tu1", "text": ROW, "content": ROW}}]},
        content_type="application/json")
    assert r.status_code == 200, r.content
    assert published and ROW not in str(published)
    assert ROW not in str(list(Message.objects.filter(session=w["hot"]).values()))


# --- read back by its own people only --------------------------------------------------

ASKED = "how many visits did Amina have?"
REPLY = "Amina has had 12 visits"


def _converse(session):
    """A turn's rows as the runner ships them (`persist_transcript_rows`)."""
    chat_transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": ASKED},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "tu1", "name": "site_call", "input": {"tool": "data_query",
                                                                   "arguments": {"q": "Amina"}}}},
        {"index": 65, "role": "tool_result", "text": ROW,
         "content": {"tool_use_id": "tu1", "is_error": False, "text": ROW}},
        {"index": 128, "role": "assistant", "text": REPLY},
    ])


def _said(page) -> str:
    return " ".join(m["plaintext"] for m in page)


def test_the_words_are_sealed_beside_metadata_columns_and_a_result_is_never_kept(w):
    _converse(w["hot"])
    rows = {m.role: m for m in Message.objects.filter(session=w["hot"])}
    assert rows[Message.USER].sealed["plaintext"] == ASKED
    assert rows[Message.ASSISTANT].sealed["plaintext"] == REPLY
    assert rows[Message.TOOL_USE].sealed["content"]["input"]["arguments"] == {"q": "Amina"}
    assert rows[Message.TOOL_RESULT].sealed is None and ROW not in str(rows[Message.TOOL_RESULT].__dict__)
    # Every column any other reader sees is metadata.
    columns = str(list(Message.objects.filter(session=w["hot"]).values("plaintext", "content")))
    assert ASKED not in columns and REPLY not in columns and "Amina" not in columns
    # An ordinary conversation seals nothing.
    _converse(w["cold"])
    assert not Message.objects.filter(session=w["cold"], sealed__isnull=False).exists()


def test_the_person_who_started_it_reads_it_back_on_the_chat_page(w):
    _converse(w["hot"])
    tail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["messages"]
    assert ASKED in _said(tail) and REPLY in _said(tail)
    assert ROW not in str(tail)  # a result is not kept for anyone
    page = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/messages?before=999").json()["messages"]
    assert REPLY in _said(page)


def test_a_participant_reads_it_back_multiplayer(w):
    from apps.canopy_sessions.models import SessionParticipant

    _converse(w["hot"])
    peer = User.objects.create_user("peer", "peer@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=peer, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.VIEWER)
    c = Client()
    c.force_login(peer)
    assert REPLY in _said(c.get(f"/api/canopy-sessions/{w['hot'].pk}").json()["messages"])


def test_no_token_or_ai_door_reads_it_back_even_for_its_own_person(w):
    from apps.tokens.models import PersonalToken

    _converse(w["hot"])
    raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    pat = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    tail = pat.get(f"/api/canopy-sessions/{w['hot'].pk}").json()["messages"]
    assert tail and ASKED not in str(tail) and REPLY not in str(tail)
    # What people typed, read back for a review, is a review: metadata, in a browser too.
    typed = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/human-inputs").json()["messages"]
    assert ASKED not in str(typed)


def test_an_mcp_call_is_not_a_chat_surface(w, rf):
    request = rf.get("/")
    request.user = w["user"]
    assert sensitivity.request_is_chat_surface(request)
    request.via_mcp = True
    assert not sensitivity.request_is_chat_surface(request)
    _converse(w["hot"])
    rows = sensitivity.readable(Message.objects.filter(session=w["hot"]), w["hot"],
                                chat_surface=False, user=w["user"])
    assert REPLY not in _said([{"plaintext": m.plaintext} for m in rows])


def test_the_socket_admits_a_browser_or_the_widget_never_a_token():
    assert sensitivity.scope_is_chat_surface({"auth_method": "session", "session": {}})
    assert sensitivity.scope_is_chat_surface({"auth_method": "dlg", "via_widget": True})
    assert not sensitivity.scope_is_chat_surface({"auth_method": "pat"})
    assert not sensitivity.scope_is_chat_surface(
        {"auth_method": "session", "session": {"_canopy_debug_session": True}})


def test_the_visitor_reads_their_own_conversation_back_in_the_widget(w):
    from apps.contacts.models import Contact
    from apps.tokens.models import AppCredential, ContactToken

    app = AppCredential.create_credential(name="connect-labs", created_by=w["user"], workspace=w["ws"])
    visitor = Contact.objects.create(workspace=w["ws"], app=app, external_id="v-1",
                                     display_name="Visitor", source=Contact.SOURCE_EMBED)
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], contact=visitor,
                               metadata={**ZDR, "embed_app": "connect-labs"})
    _converse(s)
    raw, _ = ContactToken.issue(app=app, contact=visitor, ttl_seconds=600)
    page = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(
        f"/api/contact/sessions/{s.pk}/messages?before=999").json()["messages"]
    assert ASKED in _said(page) and REPLY in _said(page)
    other = Contact.objects.create(workspace=w["ws"], app=app, external_id="v-2",
                                   display_name="Other", source=Contact.SOURCE_EMBED)
    assert not sensitivity.reads_content(s, chat_surface=True, contact=other)


# --- forced retention -------------------------------------------------------------------


def test_a_sensitive_conversation_is_purged_whatever_enforcement_says(w, settings):
    assert settings.CANOPY_RETENTION_ENFORCE is False
    cache.delete(retention._LOCK_KEY)
    old = timezone.now() - dt.timedelta(days=31)
    for s in (w["hot"], w["cold"]):
        m = Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext=ROW)
        Message.objects.filter(pk=m.pk).update(created_at=old)
    record = retention.maybe_sweep()
    assert record is not None and record.counts["totals"]["chat_messages"] == 1
    assert not Message.objects.filter(session=w["hot"]).exists()
    assert Message.objects.filter(session=w["cold"]).exists()  # no rule, not enforced
    cache.delete(retention._LOCK_KEY)
    assert retention.maybe_sweep() is None  # nothing due → no sweep recorded
    assert RetentionSweep.objects.count() == 1
    cache.delete(retention._LOCK_KEY)


def test_a_young_sensitive_conversation_is_kept(w, settings):
    settings.CANOPY_SENSITIVE_KEEP_DAYS = 30
    Message.objects.create(session=w["hot"], turn_index=0, role=Message.USER, plaintext="q")
    retention.sweep(apply=True, sensitive_only=True)
    assert Message.objects.filter(session=w["hot"]).exists()


# --- refuse re-exposure -----------------------------------------------------------------


def test_export_is_refused(w):
    Message.objects.create(session=w["hot"], turn_index=0, role=Message.USER, plaintext=ROW)
    r = w["c"].get(f"/api/canopy-sessions/{w['hot'].id}/export")
    assert r.status_code == 403 and "sensitive" in r.json()["detail"]


def test_slack_share_and_a_new_thread_are_refused(w):
    from apps.slack import share

    assert share.share_session(w["user"], channel="#dev", summary="s",
                               session=w["hot"]).status == share.SENSITIVE
    assert share.start_new_thread(w["user"], w["hot"], "s").status == share.SENSITIVE
    r = w["c"].post("/api/canopy-sessions/slack/share", data={
        "channel": "#dev", "summary": "s", "session_id": str(w["hot"].id)},
        content_type="application/json")
    assert r.status_code == 403, r.content


def test_slack_mirroring_posts_nothing(w):
    from apps.slack import relay

    t = _turn(w["hot"])
    with mock.patch.object(relay, "turn_destination") as dest, \
            mock.patch.object(relay, "session_destination") as sdest:
        assert relay.relay(t, [mock.Mock()]) == 0
        relay.on_transcript(w["hot"], [(0, "assistant", ROW)])
    dest.assert_not_called()
    sdest.assert_called_once()  # asked, then refused on sensitivity


def test_a_share_link_upload_is_refused(w):
    import io

    RunnerBinding.objects.create(session=w["hot"], runner=w["runner"], session_key="k",
                                 transcript_id="cli-abc")
    line = '{"type":"system","subtype":"init","session_id":"cli-abc"}\n'
    line += '{"type":"user","message":{"role":"user","content":"' + ROW + '"}}\n'
    f = io.BytesIO(line.encode())
    f.name = "s.jsonl"
    r = w["c"].post("/api/sessions/upload", data={"file": f})
    assert r.status_code == 403, r.content
    assert sensitivity.claude_session_is_sensitive("cli-abc")
    assert not sensitivity.claude_session_is_sensitive("cli-other")


def test_the_usage_scan_keeps_no_title(w):
    _turn(w["hot"], session_key="cs-hot")
    _turn(w["cold"], session_key="cs-cold")
    usage.record(w["runner"], [
        {"claude_session": cs, "title": ROW, "days": [{"day": "2026-10-22", "input_tokens": 1}]}
        for cs in ("cs-hot", "cs-cold")])
    assert SessionUsage.objects.get(claude_session="cs-hot").title == ""
    assert SessionUsage.objects.get(claude_session="cs-cold").title == ROW


# --- refuse re-processing ---------------------------------------------------------------


def test_auto_debug_starts_nothing(w, settings):
    from apps.harness import auto_debug

    settings.CANOPY_AUTO_DEBUG = True
    t = _turn(w["hot"], status=Turn.FAILED, result_note=ROW)
    with mock.patch.object(auto_debug, "_trigger") as trig:
        auto_debug._on_turn_failed(t)
    trig.assert_not_called()


def test_the_fleets_transcript_readers_find_nothing(w):
    t = _turn(w["hot"])
    ledger.append_transcript(t, ['{"x":"' + ROW + '"}'])
    assert ledger.read_transcript(t) == b""
