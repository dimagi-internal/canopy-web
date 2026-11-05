"""A person permanently deleting the stored words of their own session.

`DELETE /api/canopy-sessions/{id}/content` (`canopy_sessions.content_deletion`). The trap
it exists around: the rows are a cache of a file on the runner's disk, so without the
tombstone the next stream, backfill, reset or coaching upload would send it all back.
Every "dropped" test here first proves the same call stores rows on a session that was
not deleted, so none of them can pass by the ship being malformed.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import models
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import (
    attachment_storage,
    coaching,
    content_deletion,
    send,
    services,
    transcript_ingest,
    transcript_read,
)
from apps.canopy_sessions.models import (
    Attachment,
    DeletedTranscript,
    Draft,
    HandoffArchive,
    Message,
    PageAction,
    RunnerBinding,
    Session,
    SessionHandoff,
    SessionKey,
    SessionKeyWrap,
    SessionParticipant,
    SessionReviewGrant,
    SessionSecret,
    TransferRequest,
)
from apps.contacts.models import Contact, Person, PersonAuditEvent
from apps.contacts.services import person_for
from apps.events import activity, analytics
from apps.events.models import Activity, Event, SessionUsage
from apps.harness import caller_tokens, session_sync, turn_agent_tokens
from apps.harness.models import (
    FailureInvestigation,
    ProjectOptIn,
    Runner,
    Turn,
    TurnEvent,
    TurnTranscript,
)
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.token_helpers import pat_token

pytestmark = pytest.mark.django_db

KEY = "the-task"
FILE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def w():
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    bo = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    login = User.objects.create_user("trellis-login", "trellis@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=boss)
    for u, role in ((lili, "editor"), (bo, "editor"), (boss, "owner"), (login, "editor")):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    ProjectOptIn.objects.create(user=lili, projects=["canopy-web"])
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=boss, user=login)
    box = Runner.objects.create(name="lili-laptop", workspace=ws, location=Runner.LOCAL,
                                status=Runner.ONLINE, owner=lili, host="lili-mac",
                                last_heartbeat_at=timezone.now())
    return {"ws": ws, "lili": lili, "bo": bo, "boss": boss, "login": login, "trellis": trellis, "box": box}


def _laptop_session(w, key=KEY, **kw):
    kw.setdefault("project", "canopy-web")
    s = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER, title="fix the billing bug",
                               metadata={services.TRANSCRIPT_SOURCED: True, **kw.pop("metadata", {})}, **kw)
    RunnerBinding.objects.create(session=s, runner=w["box"], session_key=key, emdash_project="canopy-web",
                                 transcript_id=FILE, host="lili-mac")
    return s


def _said(session, n=3):
    for i in range(n):
        Message.objects.create(session=session, turn_index=i, role=Message.USER, plaintext=f"secret {i}",
                               content={"text": f"secret {i}"})


def _as(user):
    c = Client()
    c.force_login(user)
    return c


def _delete(user_or_client, session):
    c = user_or_client if isinstance(user_or_client, Client) else _as(user_or_client)
    return c.delete(f"/api/canopy-sessions/{session.pk}/content")


def _ship(w, session, *, transcript_id=FILE, runner=None):
    """What the runner posts for each new transcript record."""
    runner = runner or w["box"]
    return _as(runner.owner).post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(session.pk), "transcript_id": transcript_id,
              "events": [{"kind": "user", "seq": 90000, "index": 90000, "payload": {"text": "secret again"}}]},
        content_type="application/json")


def _backfill(w, session):
    return _as(w["box"].owner).post(
        f"/api/harness/runners/{w['box'].id}/session-backfill",
        data={"session_id": str(session.pk), "transcript_id": FILE,
              "messages": [{"index": 80000, "role": "user", "text": "secret again"}]},
        content_type="application/json")


def _streams(w):
    body = _as(w["box"].owner).get(f"/api/harness/runners/{w['box'].id}/streams").json()
    return {s["session_id"]: s for s in body["streams"]}


# ---------------------------------------------------------------- what it deletes

def test_every_word_goes_and_the_row_and_ids_stay(w):
    s = _laptop_session(w)
    _said(s)
    binding = s.runner_binding
    RunnerBinding.objects.filter(pk=binding.pk).update(
        tail=[{"role": "user", "text": "secret"}], summary="secret", subagents=[{"agent_id": "a", "latest": "secret"}],
        pending_question=None)
    Session.objects.filter(pk=s.pk).update(
        activity={"branches": ["secret-branch"]}, activity_keys="branch:secret-branch",
        page_state={"resource": "secret"}, origin_resource="https://x/secret")
    Attachment.objects.create(session=s, filename="secret.png", content_type="image/png", size_bytes=1,
                              storage_key="k/secret.png")
    Draft.objects.create(session=s, author=w["lili"], body="secret draft")
    PageAction.objects.create(session=s, name="do", args={"a": "secret"})
    SessionSecret.objects.create(session=s, name="TOKEN", value_zk="secret", created_by=w["lili"])
    TransferRequest.objects.create(session=s, to_runner=w["box"], brief="secret brief",
                                   status=TransferRequest.DECLINED)
    h = SessionHandoff.objects.create(session=s, brief="secret brief", manifest={"file": "secret"},
                                      settled_at=timezone.now())
    HandoffArchive.objects.create(handoff=h, data=b"secret tarball")
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="secret prompt",
                               result_note="secret note", report_summary="secret", report_title="secret",
                               idempotency_key=uuid.uuid4().hex, status=Turn.DONE, session_id="claude-a",
                               origin_ref={"thread_key": KEY, "attachments": [{"filename": "secret.png"}]},
                               work_product_urls=["https://x/secret"], initiator_user=w["lili"])
    TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": "secret"})
    TurnTranscript.objects.create(turn=turn, raw_jsonl_gz=b"secret")
    FailureInvestigation.objects.create(workspace=w["ws"], fingerprint="f", sample_note="secret",
                                        first_seen=timezone.now(), last_seen=timezone.now(),
                                        turn_ids=[str(turn.pk)])

    r = _delete(w["lili"], s)

    assert r.status_code == 200, r.content
    assert r.json()["messages"] == 3 and r.json()["attachments"] == 1 and r.json()["turns"] == 1
    for model in (Message, Attachment, Draft, PageAction, SessionSecret, HandoffArchive):
        assert not model.objects.filter(pk__isnull=False).exists(), model
    assert not TurnEvent.objects.filter(turn=turn).exists()
    assert not TurnTranscript.objects.filter(turn=turn).exists()

    s.refresh_from_db()
    binding.refresh_from_db()
    turn.refresh_from_db()
    assert s.content_deleted_at is not None and s.status == Session.ARCHIVED
    assert (s.title, s.activity, s.activity_keys, s.page_state, s.origin_resource) == ("", {}, "", {}, "")
    assert (binding.tail, binding.summary, binding.subagents) == ([], "", [])
    assert (turn.prompt, turn.result_note, turn.report_summary, turn.report_title) == ("", "", "", "")
    assert turn.work_product_urls == [] and "attachments" not in turn.origin_ref
    assert TransferRequest.objects.get(session=s).brief == ""
    assert (SessionHandoff.objects.get(session=s).brief, SessionHandoff.objects.get(session=s).manifest) == ("", None)
    assert FailureInvestigation.objects.get(fingerprint="f").sample_note == ""
    # The ids analytics and the tombstone rest on.
    assert (binding.session_key, binding.transcript_id, turn.session_id) == (KEY, FILE, "claude-a")
    assert set(DeletedTranscript.objects.values_list("transcript_id", flat=True)) == {FILE, "claude-a"}


def test_sealed_words_and_their_key_go(w):
    s = Session.objects.create(workspace=w["ws"], created_by=w["lili"], origin=Session.ORIGIN_WEB,
                               metadata={"runner_requirements": ["sensitive_data"]})
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="", sealed="cc1:ciphertext")
    SessionKey.objects.create(session=s, conversation=uuid.uuid4(), public_jwk={"kty": "EC"})
    SessionKeyWrap.objects.create(session=s, user=w["lili"], wrap="w", created_by=w["lili"])
    assert _delete(w["lili"], s).status_code == 200
    assert not Message.objects.filter(session=s).exists()
    assert not SessionKey.objects.filter(session=s).exists()
    assert not SessionKeyWrap.objects.filter(session=s).exists()
    # Still held to its requirements: its row must not stop reading as sensitive.
    s.refresh_from_db()
    assert s.metadata["runner_requirements"] == ["sensitive_data"]


def test_analytics_does_not_change(w):
    s = _laptop_session(w)
    _said(s)
    SessionUsage.objects.create(runner=w["box"], claude_session=FILE, day=dt.date.today(), session=s,
                                workspace=w["ws"], input_tokens=100, output_tokens=50, repo="canopy-web")
    activity.emit(activity.TURN_FINISHED, workspace=w["ws"].slug, actor=w["lili"], surface="web",
                  ref=f"t{s.pk}")
    before = analytics.build([w["ws"].slug])
    facts = list(Activity.objects.order_by("pk").values())
    usage = list(SessionUsage.objects.order_by("pk").values())
    assert _delete(w["lili"], s).status_code == 200
    assert analytics.build([w["ws"].slug]) == before
    assert list(Activity.objects.order_by("pk").values()) == facts
    assert list(SessionUsage.objects.order_by("pk").values()) == usage


def test_it_says_so_with_ids_only(w):
    s = _laptop_session(w)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    event = Event.objects.get(kind="session.content_deleted")
    assert event.payload["session"] == str(s.pk) and "secret" not in str(event.payload)
    assert "billing" not in str(event.payload) and "billing" not in event.summary
    audit = PersonAuditEvent.objects.get(event_type="session.contentDeleted")
    assert audit.person == person_for(user=w["lili"]) and audit.detail == str(s.pk)
    assert "billing" not in audit.purpose
    assert isinstance(audit.person, Person)


def test_deleting_again_does_nothing(w):
    s = _laptop_session(w)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    stamp = Session.objects.get(pk=s.pk).content_deleted_at
    again = _delete(w["lili"], s)
    assert again.status_code == 200 and again.json()["messages"] == 0
    assert Session.objects.get(pk=s.pk).content_deleted_at == stamp
    assert Event.objects.filter(kind="session.content_deleted").count() == 1


def test_a_file_that_will_not_delete_stops_it_and_a_retry_finishes_the_rest(w, monkeypatch):
    s = _laptop_session(w)
    _said(s)
    for key in ("first", "stuck"):
        Attachment.objects.create(session=s, filename="a.png", content_type="image/png", size_bytes=1,
                                  storage_key=key)
    monkeypatch.setattr(attachment_storage, "is_configured", lambda: True)
    gone = []

    def _delete_object(key):
        if key == "stuck":
            raise OSError("S3 is down")
        gone.append(key)

    monkeypatch.setattr(attachment_storage, "delete", _delete_object)
    r = _delete(w["lili"], s)
    assert r.status_code == 503 and "stay removed" in r.json()["detail"]
    # What the route says: the file already removed stays removed, the rest is still there.
    assert gone == ["first"]
    assert Attachment.objects.filter(session=s).count() == 2
    assert Message.objects.filter(session=s).count() == 3
    assert Session.objects.get(pk=s.pk).content_deleted_at is None

    monkeypatch.setattr(attachment_storage, "delete", gone.append)
    assert _delete(w["lili"], s).status_code == 200
    assert gone == ["first", "first", "stuck"]
    assert not Attachment.objects.filter(session=s).exists()


# ---------------------------------------------------------------- the runner must not re-upload it

def test_a_reupload_is_dropped_on_every_path(w):
    SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])
    kept, gone = _laptop_session(w, key="kept"), _laptop_session(w)
    RunnerBinding.objects.filter(session=kept).update(transcript_id="another-file")
    # The same calls store rows while nothing has been deleted.
    assert _ship(w, kept, transcript_id="another-file").status_code == 200
    assert Message.objects.filter(session=kept).exists()
    assert _backfill(w, gone).status_code == 200
    assert Message.objects.filter(session=gone).count() == 1

    assert _delete(w["lili"], gone).status_code == 200

    assert _ship(w, gone).status_code == 200
    assert _backfill(w, gone).json() == {"written": 0}
    assert transcript_ingest.persist_transcript_rows(
        gone, [{"index": 70000, "role": "user", "text": "secret"}]) == 0
    assert transcript_ingest.write_backfill(gone, [{"role": "user", "text": "secret"}]) == 0
    assert not Message.objects.filter(session=gone).exists()


def test_a_coached_box_stops_being_asked_for_it(w):
    SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])
    s = _laptop_session(w, status=Session.ARCHIVED)
    _said(s)
    assert _streams(w)[str(s.pk)]["ship_full"] is True
    assert s.pk in coaching.full_upload_session_ids(w["box"])
    assert _delete(w["lili"], s).status_code == 200
    assert str(s.pk) not in _streams(w)
    assert s.pk not in coaching.full_upload_session_ids(w["box"])


def test_a_review_grant_given_afterwards_resurrects_nothing(w):
    s = _laptop_session(w)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])
    assert str(s.pk) not in _streams(w)
    assert _ship(w, s).status_code == 200
    assert not Message.objects.filter(session=s).exists()


def test_an_open_session_stops_streaming_too(w):
    s = _laptop_session(w)
    assert str(s.pk) in _streams(w)
    assert _delete(w["lili"], s).status_code == 200
    assert str(s.pk) not in _streams(w)


def test_the_report_neither_revives_it_nor_refills_its_caches(w):
    s = _laptop_session(w)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    task = type("T", (), dict(
        emdash_task=KEY, project="canopy-web", status="idle", agent_status="", agent_status_stale=False,
        last_interacted_at=None, recent_messages=[{"role": "user", "text": "secret"}],
        question={"id": "q", "question": "secret?", "options": []}, task_uid="", subagents=[]))()
    session_sync.replace_reported_sessions(w["box"], w["ws"], [task])
    s.refresh_from_db()
    binding = RunnerBinding.objects.get(session=s)
    assert s.status == Session.ARCHIVED and s.title == ""
    assert binding.tail == [] and binding.pending_question is None
    assert "secret" not in str(s.metadata)
    assert session_sync.report_session_menu(w["box"], s.pk, {"question": "secret?"}) is True
    assert session_sync.report_session_subagents(w["box"], s.pk, [{"agent_id": "a", "latest": "secret"}]) is True
    binding.refresh_from_db()
    assert binding.pending_question is None and binding.subagents == []


def test_nothing_asks_the_runner_for_it_again(w):
    s = _laptop_session(w)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    s = Session.objects.select_related("runner_binding__runner").get(pk=s.pk)
    assert transcript_read.request_backfill(s) == "unavailable"
    RunnerBinding.objects.filter(session=s).update(backfill_requested=True)
    body = _as(w["lili"]).get(f"/api/harness/runners/{w['box'].id}/backfills").json()
    assert body["backfills"] == []
    assert services.reset_session(s)["reason"] == services.RESET_CONTENT_DELETED
    assert services.reset_sessions([s], prune_ghosts=True)["pruned"] == []
    assert Session.objects.filter(pk=s.pk).exists()


def test_the_same_transcript_under_a_new_session_row_is_dropped_too(w):
    """A box paired again: a new Runner, whose report finds no binding for the task while
    the old runner's row still holds it, files the task as a new session and ships the file."""
    gone = _laptop_session(w)
    _said(gone)
    assert _delete(w["lili"], gone).status_code == 200
    ghost_box = Runner.objects.create(name="lili-laptop-2", workspace=w["ws"], location=Runner.LOCAL,
                                      status=Runner.ONLINE, owner=w["lili"], host="lili-mac-2",
                                      last_heartbeat_at=timezone.now())
    ghost = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER, project="canopy-web",
                                   title=KEY, metadata={services.TRANSCRIPT_SOURCED: True})
    RunnerBinding.objects.create(session=ghost, runner=ghost_box, session_key=KEY, emdash_project="canopy-web",
                                 tail=[{"role": "user", "text": "secret"}])
    assert _ship(w, ghost, runner=ghost_box).status_code == 200
    ghost.refresh_from_db()
    assert not Message.objects.filter(session=ghost).exists()
    assert ghost.content_deleted_at is not None and ghost.title == ""
    assert RunnerBinding.objects.get(session=ghost).tail == []


def test_a_session_with_its_own_rows_keeps_them_when_a_deleted_transcript_lands_in_it(w):
    gone = _laptop_session(w)
    assert _delete(w["lili"], gone).status_code == 200
    other = _laptop_session(w, key="other")
    RunnerBinding.objects.filter(session=other).update(transcript_id="mine")
    _said(other)
    assert _ship(w, other).status_code == 200
    other.refresh_from_db()
    assert other.content_deleted_at is None
    assert Message.objects.filter(session=other).count() == 3


def test_a_transcript_another_session_uses_is_not_tombstoned(w):
    gone = _laptop_session(w)
    other = _laptop_session(w, key="other")  # the same FILE
    assert _delete(w["lili"], gone).status_code == 200
    assert not DeletedTranscript.objects.filter(transcript_id=FILE).exists()
    assert _ship(w, other).status_code == 200
    assert Message.objects.filter(session=other).exists()


def test_a_late_flush_of_its_turn_stays_out(w):
    from apps.harness import ledger

    s = _laptop_session(w)
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="p",
                               idempotency_key=uuid.uuid4().hex, status=Turn.DONE, initiator_user=w["lili"])
    assert _delete(w["lili"], s).status_code == 200
    ledger.append_transcript(Turn.objects.get(pk=turn.pk), ['{"secret": 1}'])
    ledger.append_events(Turn.objects.get(pk=turn.pk), [{"kind": "assistant", "payload": {"text": "secret"}}])
    assert not TurnTranscript.objects.filter(turn=turn).exists()
    assert not TurnEvent.objects.filter(turn=turn).exists()


# ---------------------------------------------------------------- it takes nothing more

def test_nothing_more_can_be_said_in_it(w):
    s = Session.objects.create(workspace=w["ws"], created_by=w["lili"], origin=Session.ORIGIN_WEB)
    _said(s)
    assert _delete(w["lili"], s).status_code == 200
    c = _as(w["lili"])
    r = c.post(f"/api/canopy-sessions/{s.pk}/send", data={"text": "hello"}, content_type="application/json")
    assert r.status_code == 409 and "deleted" in r.json()["detail"]
    assert c.post(f"/api/canopy-sessions/{s.pk}/unarchive").status_code == 409
    assert c.put(f"/api/canopy-sessions/{s.pk}/title", data={"title": "x"},
                 content_type="application/json").status_code == 409
    fresh = Session.objects.get(pk=s.pk)
    with pytest.raises(ValueError):
        send.send_message(session=fresh, text="hello", user=w["lili"])
    assert fresh.status == Session.ARCHIVED and not Message.objects.filter(session=s).exists()


# ---------------------------------------------------------------- only hers, only while quiet

def test_only_the_person_whose_session_it_is(w):
    laptop = _laptop_session(w)
    chat = Session.objects.create(workspace=w["ws"], created_by=w["lili"], origin=Session.ORIGIN_WEB)
    for s in (laptop, chat):
        _said(s)
        # A workspace owner cannot read another person's session at all; a participant's
        # refusal is the 403 below. No path through either deletes.
        assert _delete(w["bo"], s).status_code in (403, 404)
        assert _delete(w["boss"], s).status_code in (403, 404)
        assert Message.objects.filter(session=s).count() == 3
    SessionParticipant.objects.create(session=chat, user=w["bo"], role=SessionParticipant.OWNER)
    r = _delete(w["bo"], chat)
    assert r.status_code == 403 and "Only the person" in r.json()["detail"]


def test_a_workspace_owner_who_can_read_it_gets_no_override(w):
    agent_chat = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER, agent=w["trellis"])
    # The agent's own thread: its admins read and own it, and it is nobody's own session.
    _said(agent_chat)
    r = _delete(w["boss"], agent_chat)
    assert r.status_code == 403
    assert Message.objects.filter(session=agent_chat).exists()


def test_the_box_owner_keeps_it_after_it_falls_off_the_report(w):
    s = _laptop_session(w)
    _said(s)
    RunnerBinding.objects.filter(session=s).update(runner=None)
    assert _delete(w["lili"], s).status_code == 200


def test_a_contacts_conversation_is_not_hers_to_delete(w):
    contact = Contact.objects.create(email="visitor@example.com", workspace=w["ws"])
    s = Session.objects.create(workspace=w["ws"], created_by=w["lili"], contact=contact)
    _said(s)
    assert _delete(w["lili"], s).status_code == 403


def _lili_chat(w):
    s = Session.objects.create(workspace=w["ws"], created_by=w["lili"], origin=Session.ORIGIN_WEB)
    _said(s)
    return s


def test_an_agents_login_is_refused(w):
    SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])
    s = _lili_chat(w)
    r = _delete(w["login"], s)
    assert r.status_code in (403, 404)
    assert Message.objects.filter(session=s).exists()


def test_a_turns_tokens_are_refused_even_for_her_own_session(w):
    s = _lili_chat(w)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["boss"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=w["trellis"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="p",
                               idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_via="chat", initiator_assurance="session",
                               initiator_user=w["lili"])
    login_token = turn_agent_tokens.issue(turn, box)["token"]
    r = Client().delete(f"/api/canopy-sessions/{s.pk}/content", HTTP_AUTHORIZATION=f"Bearer {login_token}")
    assert r.status_code in (403, 404)

    chat_turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="p",
                                    idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=box,
                                    initiator_kind="user", initiator_via="chat", initiator_assurance="session",
                                    initiator_user=w["lili"])
    raw = caller_tokens.mint(chat_turn)
    assert raw, "the caller token this test is about"
    r = Client().delete(f"/api/canopy-sessions/{s.pk}/content", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code in (401, 403)

    # Her own PAT, naming a turn in the header a turn's token carries: refused as well.
    pat = pat_token(w["lili"])
    Turn.objects.filter(pk=chat_turn.pk).update(status=Turn.DONE)
    r = Client().delete(f"/api/canopy-sessions/{s.pk}/content", HTTP_AUTHORIZATION=f"Bearer {pat}",
                        HTTP_X_CANOPY_TURN_ID=str(chat_turn.pk))
    assert r.status_code == 403
    assert Message.objects.filter(session=s).exists()


def test_her_own_pat_with_no_turn_may(w):
    s = _lili_chat(w)
    pat = pat_token(w["lili"])
    r = Client().delete(f"/api/canopy-sessions/{s.pk}/content", HTTP_AUTHORIZATION=f"Bearer {pat}")
    assert r.status_code == 200
    assert not Message.objects.filter(session=s).exists()


def _refused_with(w, s, words):
    r = _delete(w["lili"], s)
    assert r.status_code == 409, r.content
    assert words in r.json()["detail"]
    assert Message.objects.filter(session=s).exists()
    assert Session.objects.get(pk=s.pk).content_deleted_at is None


def test_refused_while_someone_else_is_in_it(w):
    s = _lili_chat(w)
    SessionParticipant.objects.create(session=s, user=w["bo"], role=SessionParticipant.VIEWER)
    _refused_with(w, s, "theirs too")


def test_refused_when_someone_taken_out_of_it_wrote_in_it(w):
    s = _lili_chat(w)
    Message.objects.create(session=s, turn_index=10, role=Message.USER, plaintext="hi",
                           author={"name": "Bo", "user_id": w["bo"].pk})
    _refused_with(w, s, "theirs too")


def test_her_own_authored_lines_do_not_count_as_someone_else(w):
    s = _lili_chat(w)
    Message.objects.create(session=s, turn_index=10, role=Message.USER, plaintext="hi",
                           author={"name": "Lili", "user_id": w["lili"].pk})
    assert _delete(w["lili"], s).status_code == 200


def test_refused_when_someone_else_asked_a_turn_in_it(w):
    s = _lili_chat(w)
    Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="p", status=Turn.DONE,
                        idempotency_key=uuid.uuid4().hex, initiator_user=w["bo"])
    _refused_with(w, s, "theirs too")


def test_refused_while_it_lives_in_a_slack_thread(w):
    s = _lili_chat(w)
    Session.objects.filter(pk=s.pk).update(metadata={"slack_thread": {"channel": "C1", "ts": "1.2"}})
    _refused_with(w, s, "theirs too")


def test_refused_while_a_turn_is_open(w):
    s = _lili_chat(w)
    for status in (Turn.QUEUED, Turn.RUNNING):
        turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="p", status=status,
                                   idempotency_key=uuid.uuid4().hex, initiator_user=w["lili"])
        _refused_with(w, s, "still working")
        Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _delete(w["lili"], s).status_code == 200


def test_refused_while_its_engine_says_it_is_working(w):
    s = _laptop_session(w)
    _said(s)
    RunnerBinding.objects.filter(session=s).update(agent_status="working", live_seen_at=timezone.now(),
                                                   reported_at=timezone.now())
    _refused_with(w, s, "still working")


def test_refused_while_it_is_moving(w):
    s = _laptop_session(w)
    _said(s)
    h = SessionHandoff.objects.create(session=s)
    _refused_with(w, s, "being moved")
    SessionHandoff.objects.filter(pk=h.pk).update(settled_at=timezone.now())
    TransferRequest.objects.create(session=s, to_runner=w["box"], status=TransferRequest.PENDING)
    _refused_with(w, s, "being moved")


def test_refused_while_a_fork_holds_a_copy(w):
    s = _lili_chat(w)
    fork = Session.objects.create(workspace=w["ws"], created_by=w["lili"], parent_session=s,
                                  metadata={"fork": {"session_key": KEY, "records": 1}})
    _refused_with(w, s, "fork")
    assert _delete(w["lili"], fork).status_code == 200
    assert _delete(w["lili"], s).status_code == 200


# ---------------------------------------------------------------- the coach's read paths

def _coach_turn_token(w):
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["boss"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=w["trellis"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="coach",
                               idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_via="chat", initiator_assurance="session",
                               initiator_user=w["lili"])
    return turn_agent_tokens.issue(turn, box)["token"]


def test_a_coach_reads_nothing_of_it_afterwards(w):
    SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])
    gone, kept = _lili_chat(w), _lili_chat(w)
    bearer = {"HTTP_AUTHORIZATION": f"Bearer {_coach_turn_token(w)}"}

    def get(path):
        return Client().get(path, **bearer)

    # The read this all rests on works before the delete.
    assert get(f"/api/canopy-sessions/{gone.pk}").status_code == 200
    assert get(f"/api/canopy-sessions/{gone.pk}/messages?before=99").status_code == 200
    assert str(gone.pk) in str(get("/api/canopy-sessions/?state=all").json())

    assert _delete(w["lili"], gone).status_code == 200

    assert get(f"/api/canopy-sessions/{gone.pk}").status_code == 404
    assert get(f"/api/canopy-sessions/{gone.pk}/messages?before=99").status_code == 404
    assert get(f"/api/canopy-sessions/{gone.pk}/export").status_code == 404
    listed = str(get("/api/canopy-sessions/?state=all").json())
    assert str(gone.pk) not in listed and str(kept.pk) in listed
    assert str(gone.pk) not in str(get("/api/canopy-sessions/search").json())
    # A review grant has not been touched, and her other session still reads.
    assert get(f"/api/canopy-sessions/{kept.pk}").status_code == 200


def test_a_laptop_sessions_task_name_is_not_served_afterwards(w):
    s = _laptop_session(w)
    _said(s)
    assert _as(w["lili"]).get(f"/api/canopy-sessions/{s.pk}").json()["session_key"] == KEY
    assert _delete(w["lili"], s).status_code == 200
    body = _as(w["lili"]).get(f"/api/canopy-sessions/{s.pk}").json()
    assert body["title"] == "" and body["session_key"] == ""
    assert RunnerBinding.objects.get(session=s).session_key == KEY


def test_she_sees_the_empty_row_and_that_it_was_deleted(w):
    s = _lili_chat(w)
    assert _as(w["lili"]).get(f"/api/canopy-sessions/{s.pk}").json()["can_delete_content"] is True
    assert _delete(w["lili"], s).status_code == 200
    body = _as(w["lili"]).get(f"/api/canopy-sessions/{s.pk}").json()
    assert body["content_deleted_at"] and body["title"] == "" and body["my_role"] == "owner"
    assert body["session_key"] == ""
    assert body["messages"] == []


# ---------------------------------------------------------------- no table goes unplaced

#: Every model pointing at a Session or a Turn, and what `delete_content` does about it.
#: A new table fails `test_every_table_pointing_at_a_session_or_turn_is_placed` until it is
#: placed here, which is the moment to decide whether it holds what was said.
WORDS = {  # holds what was said: emptied (or dropped) by `content_deletion`
    "canopy_sessions.Message", "canopy_sessions.Attachment", "canopy_sessions.Draft",
    "canopy_sessions.PageAction", "canopy_sessions.SessionSecret", "canopy_sessions.SessionKey",
    "canopy_sessions.SessionKeyWrap", "canopy_sessions.RunnerBinding", "canopy_sessions.TransferRequest",
    "canopy_sessions.SessionHandoff", "canopy_sessions.Session", "canopy_sessions.AppViewSubscription",
    "canopy_sessions.ChatKey",
    "agents.AgentSecretRequest", "harness.ActAsGrant", "harness.CallerToken", "harness.PendingMail",
    "harness.Turn", "harness.TurnEvent", "harness.TurnTranscript", "harness.FailureInvestigation",
    "admission.AdmissionNotice",
    "slack.SlackMenuPost", "slack.SlackChoicePost", "slack.SlackTurnPost",
}
IDS_ONLY = {  # ids, switches and timestamps; nothing anyone said
    "canopy_sessions.SessionParticipant", "canopy_sessions.JoinRequest", "canopy_sessions.SessionReviewAsk",
    "harness.TurnAgentToken",
    "events.SessionUsage", "contacts.SessionAgentMemory", "contacts.PersonAccess",
    "slack.SlackAudienceMember", "slack.SlackTranscriptPost", "slack.SlackRelayPost",
    "capabilities.CapabilityLease",
}
OWN_RECORD = {  # a thing someone made and has its own delete; it only says which session it came from
    "walkthroughs.Walkthrough", "reviews.ReviewRequest", "storyboards.Storyboard",  # published artifacts
    "agents.AgentTask", "agent_tasks.AgentTask", "huddles.CheckinRun", "harness.RunnerDrill",
    "contacts.PersonFact", "contacts.PersonAuditEvent", "contacts.PersonSearch",  # the person's memory page
}


def test_every_table_pointing_at_a_session_or_turn_is_placed():
    placed = WORDS | IDS_ONLY | OWN_RECORD
    found = set()
    for model in django_apps.get_models():
        for field in model._meta.get_fields():
            if (isinstance(field, models.ForeignKey | models.OneToOneField)
                    and field.related_model in (Session, Turn)):
                found.add(model._meta.label)
    assert found - placed == set(), "place these in this test, and clear them if they hold words"
    assert placed - found == set(), "no longer point at a session or a turn"


def test_a_refusal_string_exists_for_every_blocker():
    assert set(content_deletion.REFUSALS) == {content_deletion.RUNNING, content_deletion.MOVING,
                                              content_deletion.HAS_FORK}
