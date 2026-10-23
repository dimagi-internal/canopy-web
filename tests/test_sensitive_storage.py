"""A sensitive conversation (it requires sensitive-data runners) keeps the host's data inside
the boundary: canopy stores less, purges on its own clock, and never re-exposes or
re-processes it. One test per surface.

Spec: docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §4.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.events import usage
from apps.events.models import SessionUsage
from apps.harness import ledger, sensitivity
from apps.harness.models import Runner, Turn, TurnEvent, TurnTranscript
from apps.retention import services as retention
from apps.retention.models import RetentionSweep
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import (
    ASKED,
    REPLY,
    ROW,
    SENSITIVE,
    _converse,
    _socket,
    _turn,
    _unowned_thread,
    browser_said,
    open_sealed,
    rewrap_for,
    vault_key,
    world,
)

pytestmark = pytest.mark.django_db

@pytest.fixture()
def w():
    return world()


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

def _said(page) -> str:
    return " ".join(m["plaintext"] for m in page)


def test_the_words_are_sealed_beside_metadata_columns_and_a_result_is_never_kept(w):
    _converse(w["hot"])
    rows = {m.role: m for m in Message.objects.filter(session=w["hot"])}
    # Sealed to the conversation's key: its person's key opens them, as a browser would.
    def opened(role):
        return open_sealed(rows[role].sealed, w["hot"], w["key"], w["user"])

    assert opened(Message.USER)["plaintext"] == ASKED
    assert opened(Message.ASSISTANT)["plaintext"] == REPLY
    assert opened(Message.TOOL_USE)["content"]["input"]["arguments"] == {"q": "Amina"}
    assert rows[Message.TOOL_RESULT].sealed is None and ROW not in str(rows[Message.TOOL_RESULT].__dict__)
    # Every column — `sealed` included, which is ciphertext — is unreadable to a
    # database read, a backup or a shell.
    columns = str(list(Message.objects.filter(session=w["hot"]).values("plaintext", "content", "sealed")))
    assert ASKED not in columns and REPLY not in columns and "Amina" not in columns
    # An ordinary conversation seals nothing.
    _converse(w["cold"])
    assert not Message.objects.filter(session=w["cold"], sealed__isnull=False).exists()


def test_the_person_who_started_it_reads_it_back_on_the_chat_page(w):
    """The server hands its own person the ciphertext and their wrap; their browser
    reads it. The response itself carries none of the words."""
    _converse(w["hot"])
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    tail, key = detail["messages"], detail["sealed_key"]
    assert ASKED not in str(detail) and REPLY not in str(detail)
    assert key["current"] and key["wrap"]["alg"] == "ecdh-es"
    said = browser_said(tail, key, w["key"], w["user"].pk)
    assert ASKED in said and REPLY in said
    assert ROW not in str(tail)  # a result is not kept for anyone
    page = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/messages?before=999").json()["messages"]
    assert REPLY in browser_said(page, key, w["key"], w["user"].pk)


def test_a_participant_reads_it_back_multiplayer(w):
    """Added from a browser already in it, which wraps the key to their vault key."""
    _converse(w["hot"])
    peer = User.objects.create_user("peer", "peer@dimagi.com", "pw")
    peer_key = vault_key(peer)
    WorkspaceMembership.objects.create(user=peer, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    r = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/participants", data={
        "email": "peer@dimagi.com", "role": "viewer",
        "wrap": rewrap_for(w["hot"], w["user"], w["key"], peer)}, content_type="application/json")
    assert r.status_code == 200, r.content
    c = Client()
    c.force_login(peer)
    detail = c.get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], peer_key, peer.pk)


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


# The socket's own door — a browser, the widget, never a token or a minted cookie — is
# driven through the real `RealtimeAuthMiddleware` in tests/test_sealed_server_doors.py.


def test_a_visitor_cannot_hold_one_and_nothing_of_its_words_is_kept(w):
    """A contact has no canopy account and so no vault key: they cannot start one, and
    a sensitive conversation that has a contact and no key keeps no words at all."""
    from apps.contacts.models import Contact
    from apps.tokens.models import AppCredential, ContactToken

    app = AppCredential.create_credential(name="connect-labs", created_by=w["user"], workspace=w["ws"])
    visitor = Contact.objects.create(workspace=w["ws"], app=app, external_id="v-1",
                                     display_name="Visitor", source=Contact.SOURCE_EMBED)
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], contact=visitor,
                               metadata={**SENSITIVE, "embed_app": "connect-labs"})
    _converse(s)
    assert not Message.objects.filter(session=s, sealed__isnull=False).exists()
    raw, _ = ContactToken.issue(app=app, contact=visitor, ttl_seconds=600)
    visitor_client = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    page = visitor_client.get(f"/api/contact/sessions/{s.pk}/messages?before=999").json()["messages"]
    assert ASKED not in str(page) and REPLY not in str(page)
    r = visitor_client.post(f"/api/contact/sessions/{s.pk}/send", data={"text": "hello"},
                            content_type="application/json")
    assert r.status_code == 422 and "vault key" in r.json()["detail"]


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


# --- every live and log door -------------------------------------------------------------


def test_the_live_socket_is_its_own_peoples_alone(w):
    """The live frames carry the words, so the socket gate is `reads_content`."""
    assert _socket(w["hot"], w["user"])
    assert not _socket(w["hot"], w["user"], auth_method="pat")
    assert _socket(w["cold"], w["user"], auth_method="pat")
    # The agent's owner may read a thread nobody started, but is not in it.
    assert not _socket(_unowned_thread(w), w["user"])


def test_someone_not_in_it_can_neither_join_nor_copy_it(w):
    s = _unowned_thread(w)
    _converse(s)
    r = w["c"].post(f"/api/canopy-sessions/{s.pk}/participants",
                    data={"email": "jj@dimagi.com", "role": "viewer"}, content_type="application/json")
    assert r.status_code == 403, r.content
    r = w["c"].post(f"/api/canopy-sessions/{s.pk}/fork", data={"turn_index": 128},
                    content_type="application/json")
    assert r.status_code == 403, r.content


def test_the_turn_log_tail_and_reads_never_carry_the_words(w, monkeypatch):
    from apps.harness import turn_access

    t = _turn(w["hot"], prompt=ASKED)
    assert not turn_access.can_read_turn_content(w["user"], t)
    body = w["c"].get(f"/api/harness/turns/{t.pk}").json()
    assert body["content_hidden"] and ASKED not in str(body)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    with mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        ledger.append_events(t, [{"kind": "assistant_text", "payload": {"text": REPLY}}])
    tail = [m for g, m in published if g.startswith("turn.")]
    assert tail and REPLY not in str(tail)


def test_the_creation_log_line_keeps_no_prompt(w):
    from apps.harness import provenance

    assert provenance.turn_record(_turn(w["hot"], prompt=ASKED))["prompt_head"] == ""
    assert provenance.turn_record(_turn(w["cold"], prompt=ASKED))["prompt_head"] == ASKED


def test_the_envelope_never_hands_the_words_on(w):
    """A sensitive conversation's agent resumes the session its box kept; canopy-web
    never reads the sealed words back to give the next turn (spec 2026-10-23 §1)."""
    from apps.harness import caller_context

    _converse(w["hot"])
    t = _turn(w["hot"], prompt="and last month?")
    env = caller_context.build(t)
    assert "history" not in env["conversation"]
    assert ASKED not in str(env) and REPLY not in str(env)


def test_the_claim_says_the_conversation_is_sensitive(w):
    from apps.harness.schemas import ClaimedTurnOut

    assert ClaimedTurnOut.resolve_sensitive_conversation(_turn(w["hot"])) is True
    assert ClaimedTurnOut.resolve_sensitive_conversation(_turn(w["cold"])) is False


def test_the_chat_page_is_told(w):
    assert w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["sensitive"] is True
    assert w["c"].get(f"/api/canopy-sessions/{w['cold'].pk}").json()["sensitive"] is False



# --- a confined turn's reply reaches its conversation -----------------------------------


def _ship(w, runner, transcript_id, rows):
    return w["c"].post(f"/api/harness/runners/{runner.id}/session-stream", data={
        "session_id": str(w["hot"].id), "transcript_id": transcript_id,
        "events": [{"kind": k, "seq": i, "index": i, "payload": {"text": t}}
                   for i, (k, t) in enumerate(rows)]}, content_type="application/json")


def test_a_confined_turns_reply_arrives_with_no_binding_and_each_turn_lands_after_the_last(w):
    """A confined turn is never the session's binding; its runner ships the turn's rows
    at its end, and they are the only way the reply reaches the asker (seen live
    2026-10-23: `session not bound to this runner`, and the visitor got nothing)."""
    from apps.canopy_sessions.services import TRANSCRIPT_SOURCED

    w["hot"].metadata = {**w["hot"].metadata, TRANSCRIPT_SOURCED: True}
    w["hot"].save(update_fields=["metadata"])
    first = _turn(w["hot"], prompt=ASKED, capability="data", claimed_by=w["runner"],
                  status=Turn.RUNNING)
    assert _ship(w, w["runner"], "cli-1", [("user", ASKED), ("assistant", REPLY)]).status_code == 200
    first.status, first.finished_at = Turn.DONE, timezone.now()
    first.save(update_fields=["status", "finished_at"])
    _turn(w["hot"], prompt="and last month?", capability="data", claimed_by=w["runner"],
          status=Turn.RUNNING)
    # The next turn is a fresh Claude session: its rows start at ordinal 0 again.
    assert _ship(w, w["runner"], "cli-2", [("user", "and last month?"),
                                           ("assistant", "9 visits")]).status_code == 200
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    said = browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)
    assert REPLY in said and "9 visits" in said
    # Still sealed: nothing readable in the columns.
    assert REPLY not in str(list(Message.objects.filter(session=w["hot"]).values("plaintext", "content")))


def test_a_resumed_session_reships_its_transcript_and_only_the_new_turn_lands(w):
    """A sensitive conversation's next turn resumes the SAME Claude session (spec
    2026-10-23 §1), so its turn-end ship carries the whole transcript again, under the
    same id and with the same ordinals for the earlier turns. Those are already held:
    only the new turn's rows are added, and nothing is doubled."""
    from apps.canopy_sessions.services import TRANSCRIPT_SOURCED

    w["hot"].metadata = {**w["hot"].metadata, TRANSCRIPT_SOURCED: True}
    w["hot"].save(update_fields=["metadata"])
    first = _turn(w["hot"], prompt=ASKED, capability="data", claimed_by=w["runner"],
                  status=Turn.RUNNING)
    turn_one = [("user", ASKED), ("assistant", REPLY)]
    assert _ship(w, w["runner"], "cli-1", turn_one).status_code == 200
    held = list(Message.objects.filter(session=w["hot"]).order_by("turn_index")
                .values_list("turn_index", flat=True))
    first.status, first.finished_at = Turn.DONE, timezone.now()
    first.save(update_fields=["status", "finished_at"])
    _turn(w["hot"], prompt="and last month?", capability="data", claimed_by=w["runner"],
          status=Turn.RUNNING)
    r = _ship(w, w["runner"], "cli-1", turn_one + [("user", "and last month?"),
                                                   ("assistant", "9 visits")])
    assert r.status_code == 200
    rows = list(Message.objects.filter(session=w["hot"]).order_by("turn_index"))
    assert [m.turn_index for m in rows[:len(held)]] == held and len(rows) == len(held) + 2
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    said = browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)
    assert said.count(REPLY) == 1 and said.count("9 visits") == 1
    # The same ship again (a retry) adds nothing.
    assert _ship(w, w["runner"], "cli-1", turn_one + [("user", "and last month?"),
                                                      ("assistant", "9 visits")]).status_code == 200
    assert Message.objects.filter(session=w["hot"]).count() == len(rows)


def test_live_ships_during_a_confined_turn_then_the_turn_end_ship_adds_and_shows_nothing_twice(
        w, monkeypatch):
    """The runner tails a running confined turn and ships its rows as Claude writes
    them, through the same door and transcript id as the turn-end ship. Each row is
    kept once and shown once, the moment it first arrives; the turn-end ship then
    lands on rows already held and fans nothing out again."""
    from apps.canopy_sessions.services import TRANSCRIPT_SOURCED

    w["hot"].metadata = {**w["hot"].metadata, TRANSCRIPT_SOURCED: True}
    w["hot"].save(update_fields=["metadata"])
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _turn(w["hot"], prompt=ASKED, capability="data", claimed_by=w["runner"],
          status=Turn.RUNNING)
    live_one = [("user", ASKED), ("assistant", "Looking now.")]
    assert _ship(w, w["runner"], "cli-1", live_one).status_code == 200
    assert [m["event"]["payload"]["text"] for m in published] == [ASKED, "Looking now."]
    assert _ship(w, w["runner"], "cli-1", live_one + [("assistant", REPLY)]).status_code == 200
    assert [m["event"]["payload"]["text"] for m in published][2:] == [REPLY]
    held = Message.objects.filter(session=w["hot"]).count()
    published.clear()
    # The turn ends: the whole transcript again, as the final flush.
    assert _ship(w, w["runner"], "cli-1", live_one + [("assistant", REPLY)]).status_code == 200
    assert published == [] and Message.objects.filter(session=w["hot"]).count() == held == 3
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    said = browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)
    assert said.count(REPLY) == 1 and said.count("Looking now.") == 1


def _kept(w, runner, ids):
    return w["c"].post(f"/api/harness/runners/{runner.id}/kept-conversations",
                       data={"session_ids": [str(i) for i in ids]}, content_type="application/json")


def test_a_box_learns_which_of_its_kept_conversations_are_closed(w):
    """A confined conversation has no binding, so `/closes` never names it: its box
    asks, and forgets the session of every conversation not still open."""
    from apps.canopy_sessions.control import close_session

    other = Runner.objects.create(name="other", workspace=w["ws"], location=Runner.LOCAL,
                                  status=Runner.ONLINE, owner=w["user"])
    _turn(w["hot"], capability="data", claimed_by=w["runner"], status=Turn.DONE)
    _turn(w["cold"], capability="data", claimed_by=other, status=Turn.DONE)
    r = _kept(w, w["runner"], [w["hot"].pk, w["cold"].pk, "not-a-uuid"])
    assert r.status_code == 200, r.content
    # Open, and this runner ran a confined turn in it; never another box's conversation.
    assert r.json() == {"open": [str(w["hot"].pk)]}
    assert close_session(session=w["hot"]) == "closed"
    assert _kept(w, w["runner"], [w["hot"].pk]).json() == {"open": []}


def test_only_the_runner_that_ran_a_confined_turn_may_ship_its_rows_and_only_for_a_while(w):
    other = Runner.objects.create(name="other", workspace=w["ws"], location=Runner.LOCAL,
                                  status=Runner.ONLINE, owner=w["user"])
    # No turn on this runner at all.
    assert _ship(w, w["runner"], "cli-1", [("assistant", REPLY)]).status_code == 404
    # An unconfined turn is the binding's business, not this door's.
    t = _turn(w["hot"], claimed_by=w["runner"], status=Turn.RUNNING)
    assert _ship(w, w["runner"], "cli-1", [("assistant", REPLY)]).status_code == 404
    t.delete()
    # A confined turn another runner ran.
    t = _turn(w["hot"], capability="data", claimed_by=other, status=Turn.RUNNING)
    assert _ship(w, w["runner"], "cli-1", [("assistant", REPLY)]).status_code == 404
    t.delete()
    # This runner's confined turn, ended long ago.
    _turn(w["hot"], capability="data", claimed_by=w["runner"], status=Turn.DONE,
          finished_at=timezone.now() - dt.timedelta(hours=1))
    assert _ship(w, w["runner"], "cli-1", [("assistant", REPLY)]).status_code == 404


def test_a_sensitive_turns_ledger_reads_as_a_log_of_metadata(w):
    """What troubleshooting needs is readable; the words are not (2026-10-23: a
    sensitive turn's events 404'd, and its reply could not be debugged)."""
    t = _turn(w["hot"], prompt=ASKED)
    ledger.append_events(t, [{"kind": "assistant_text", "payload": {"text": REPLY}},
                             {"kind": "tool_start", "payload": {"name": "site_call",
                                                                "input": {"tool": "data_query"}}}])
    r = w["c"].get(f"/api/harness/turns/{t.pk}/events")
    assert r.status_code == 200, r.content
    events = r.json()["events"]
    assert [e["kind"] for e in events] == ["assistant_text", "tool_start"]
    assert events[1]["payload"]["name"] == "site_call" and events[1]["payload"]["input_tool"] == "data_query"
    assert REPLY not in str(events) and ASKED not in str(events)
    # The turn's words stay withheld everywhere else.
    assert w["c"].get(f"/api/harness/turns/{t.pk}").json()["content_hidden"] is True
    # And a person who could not read the turn at all still cannot.
    outsider = User.objects.create_user("out", "out@x.org", "pw")
    c = Client()
    c.force_login(outsider)
    assert c.get(f"/api/harness/turns/{t.pk}/events").status_code == 404
