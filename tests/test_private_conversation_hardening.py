"""A re-audit of private (sensitive) conversations, each finding driven through its
real entry point: forking metadata, becoming private without a key, the unrun-turn
deadline, turns linked by `canopy_session`, files from before, key copies, a private
parent, drafts and events racing the switch, and forks made before it.

docs/architecture/tenancy.md § Sensitive conversations. The browser half (a changed
vault key stops a new chat; a changed person's key needs its fingerprint typed) is in
`frontend/src/vault/*.test.ts(x)`; the Slack "Run on cloud" refusal is in
`tests/test_slack.py`.
"""
from __future__ import annotations

import datetime as dt
from io import BytesIO
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import drafts, services
from apps.canopy_sessions.models import (
    Attachment,
    Message,
    RunnerBinding,
    Session,
    SessionKey,
    SessionKeyWrap,
    SessionParticipant,
)
from apps.harness import ledger, sensitivity, session_sync
from apps.harness.models import Runner, Turn, TurnEvent, TurnTranscript
from apps.workspaces.models import WorkspaceMembership
from canopy_transcript import BLOCK_STRIDE as S
from tests.fixtures.sensitive import (
    ASKED,
    REPLY,
    ROW,
    _converse,
    _turn,
    browser_said,
    rewrap_for,
    vault_key,
    world,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(db):
    return world()


def _member(w, name, role=WorkspaceMembership.EDITOR, *, key=True):
    user = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=w["ws"], role=role)
    priv = vault_key(user) if key else None
    c = Client()
    c.force_login(user)
    return user, priv, c


def _in_it(w, user, priv, session, role=SessionParticipant.EDITOR):
    """`user` added to `session` the way a browser in it does: a participant with a copy."""
    SessionParticipant.objects.create(session=session, user=user, role=role)
    give = rewrap_for(session, w["user"], w["key"], user)
    SessionKeyWrap.objects.create(session=session, user=user, recipient_jkt=give.pop("recipient_jkt"),
                                  wrap=give, created_by=w["user"])


# --- 1. a fork is canopy's to make -------------------------------------------------------


def _bound(w, session, key="src-sid"):
    session.metadata = {**(session.metadata or {}), services.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    runner = Runner.objects.filter(pk=w["runner"].pk).first()
    Runner.objects.filter(pk=runner.pk).update(kind=Runner.CLOUD, capabilities={"sessions": True})
    return RunnerBinding.objects.create(session=session, runner=runner, session_key=key,
                                        thread_key=str(session.id))


def test_a_member_cannot_mark_their_chat_a_fork_of_someone_elses(w):
    victim = w["cold"]
    _bound(w, victim)
    mallory, _priv, mc = _member(w, "mallory")
    r = mc.post("/api/canopy-sessions/", {
        "agent_slug": w["agent"].slug,
        "metadata": {"fork": {"worktree_of": str(victim.id), "session_key": "src-sid", "records": 99,
                              "from_session": str(victim.id), "index_offsets": "junk"},
                     "origin_key": "mine"}}, content_type="application/json")
    assert r.status_code == 200, r.content
    mine = Session.objects.get(pk=r.json()["id"])
    assert "fork" not in mine.metadata and mine.metadata["origin_key"] == "mine"
    # What the runner is told for its turns: nothing about anyone else's directory.
    plan = session_sync.resolve_session(w["agent"], str(mine.id), w["runner"])
    assert plan["fork"] is None and plan["worktree_of"] == "" and plan["fork_lineage"] == []
    # And the browser's index check stays on for it.
    detail = mc.get(f"/api/canopy-sessions/{mine.pk}").json()
    assert (detail.get("sealed_key") or {}).get("index_offsets", []) == []


def test_a_fork_runs_only_in_a_directory_of_its_own_lineage(w):
    """Whatever metadata a row holds, `worktree_of` is honoured only up canopy's own
    `parent_session` column, and the cut only from the recorded parent Claude session."""
    victim = w["cold"]
    _bound(w, victim, key="victim-sid")
    other = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"])
    planted = Session.objects.create(
        workspace=w["ws"], agent=w["agent"], created_by=w["user"], parent_session=other,
        parent_claude_session="other-sid",
        metadata={"fork": {"worktree_of": str(victim.id), "session_key": "victim-sid", "records": 4}})
    plan = session_sync.resolve_session(w["agent"], str(planted.id), w["runner"])
    assert plan["worktree_of"] == str(planted.id)
    assert plan["fork_lineage"] == [str(planted.id), str(other.id)]
    assert plan["fork"] is None  # the named cut is not the recorded parent's session


def test_a_real_fork_names_its_source_and_its_lineage(w):
    owner_c = w["c"]
    source = w["cold"]
    _bound(w, source)
    for i, role in ((0, Message.USER), (S, Message.ASSISTANT)):
        Message.objects.create(session=source, turn_index=i, role=role, plaintext=f"row {i}")
    r = owner_c.post(f"/api/canopy-sessions/{source.pk}/fork", {"turn_index": 0},
                     content_type="application/json")
    assert r.status_code == 200, r.content
    plan = session_sync.resolve_session(w["agent"], r.json()["id"], w["runner"])
    assert plan["worktree_of"] == str(source.id)
    assert plan["fork_lineage"] == [r.json()["id"], str(source.id)]
    assert plan["fork"] == {"session_key": "src-sid", "records": 2}


def test_an_owner_forks_their_private_conversation(w):
    """The existing door test checks only `!= 403`; this one checks it really forks,
    and the fork is private with its rows still readable by its owner."""
    hot = w["hot"]
    _bound(w, hot)
    _converse(hot)
    r = w["c"].post(f"/api/canopy-sessions/{hot.pk}/fork", {"turn_index": 2 * 64},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    fork = Session.objects.get(pk=r.json()["id"])
    assert sensitivity.is_sensitive(fork) and fork.parent_session_id == hot.pk
    assert SessionKey.objects.get(session=fork).conversation == SessionKey.objects.get(session=hot).conversation
    detail = w["c"].get(f"/api/canopy-sessions/{fork.pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)


# --- 3. a follow-up queued behind a long turn is not expired --------------------------------


def _tick():
    from apps.common import ticks

    ticks.jobs()["harness.sensitive_turn_deadline"].fn()


def test_a_follow_up_waits_out_a_long_turn_and_its_clock_starts_when_it_could_run(w):
    hot = w["hot"]
    two_hours = timezone.now() - dt.timedelta(hours=2)
    running = _turn(hot, prompt=ASKED, status=Turn.RUNNING, claimed_by=w["runner"],
                    started_at=two_hours)
    queued = _turn(hot, prompt="and the next one?", status=Turn.QUEUED)
    Turn.objects.filter(pk__in=[running.pk, queued.pk]).update(created_at=two_hours)
    _tick()
    queued.refresh_from_db()
    assert (queued.status, queued.prompt) == (Turn.QUEUED, "and the next one?")
    # The long turn ends ten minutes ago: the follow-up's hour starts then.
    Turn.objects.filter(pk=running.pk).update(status=Turn.DONE,
                                              finished_at=timezone.now() - dt.timedelta(minutes=10))
    _tick()
    queued.refresh_from_db()
    assert queued.status == Turn.QUEUED
    # An hour and more after it could first run, it is expired as before.
    Turn.objects.filter(pk=running.pk).update(finished_at=timezone.now() - dt.timedelta(minutes=70))
    _tick()
    queued.refresh_from_db()
    assert (queued.status, queued.prompt) == (Turn.MISSED, "")


# --- 5. turns linked through `canopy_session` are held to the seal ---------------------------


def _agent_turn(w, session, **kw):
    import uuid

    return Turn.objects.create(agent=w["agent"], canopy_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=uuid.uuid4().hex, **kw)


def test_an_agent_turn_that_ran_in_a_conversation_is_held_to_its_seal(w):
    cold = w["cold"]
    done = _agent_turn(w, cold, prompt=ASKED, status=Turn.DONE, result_note=f"read {ROW}")
    TurnEvent.objects.create(turn=done, seq=1, kind="assistant", payload={"text": REPLY})
    TurnTranscript.objects.create(turn=done, raw_jsonl_gz=ROW.encode(), line_count=1)
    live = _agent_turn(w, cold, prompt="board item: call Amina", status=Turn.RUNNING,
                       claimed_by=w["runner"])

    services.add_runner_requirements(cold, {"sensitive_data"})

    done.refresh_from_db()
    assert done.prompt == "" and ROW not in done.result_note
    assert REPLY not in str(list(TurnEvent.objects.filter(turn=done).values_list("payload", flat=True)))
    assert not TurnTranscript.objects.filter(turn=done).exists()
    # The one still running is sensitive now: it routes to a sensitive-data box only,
    # its new events are metadata, and its prompt goes when it ends.
    live = Turn.objects.get(pk=live.pk)
    assert sensitivity.is_sensitive(live)
    ledger.append_events(live, [{"kind": "assistant", "payload": {"text": REPLY}}])
    assert REPLY not in str(list(TurnEvent.objects.filter(turn=live).values_list("payload", flat=True)))
    from apps.harness.closing import close_turn

    close_turn(live, Turn.DONE, "done", from_states=[Turn.RUNNING])
    live.refresh_from_db()
    assert live.prompt == ""


def test_an_agent_turn_of_a_private_conversation_is_not_claimed_by_an_ordinary_box(w):
    from apps.harness import runner_requirements as rr

    queued = _agent_turn(w, w["hot"], prompt="follow up", status=Turn.QUEUED)
    assert rr.SENSITIVE_DATA in rr.requirements_of(queued)
    assert not rr.runner_satisfies(w["runner"], rr.requirements_of(queued))


# --- 6. files attached before it became private --------------------------------------------


def test_files_from_before_are_deleted_when_it_becomes_private_and_none_is_claimed(
        w, settings, django_capture_on_commit_callbacks):
    settings.CANOPY_ATTACHMENTS_BUCKET = "test-bucket"
    cold = w["cold"]
    png = BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    png.name = "chart.png"
    with mock.patch("apps.canopy_sessions.attachment_storage.put"):
        up = w["c"].post(f"/api/canopy-sessions/{cold.pk}/attachments", {"file": png})
    assert up.status_code == 201, up.content
    sent = Attachment.objects.create(session=cold, uploaded_by=w["user"], filename="old.png",
                                     content_type="image/png", size_bytes=3, storage_key="k/old",
                                     sent_at=timezone.now())
    keys = set(Attachment.objects.filter(session=cold).values_list("storage_key", flat=True))

    with mock.patch("apps.canopy_sessions.attachment_storage.delete") as delete, \
            django_capture_on_commit_callbacks(execute=True):
        services.add_runner_requirements(cold, {"sensitive_data"})

    assert not Attachment.objects.filter(session=cold).exists()
    assert {c.args[0] for c in delete.call_args_list} == keys
    # Neither the person's read nor a runner's reaches a file that was there.
    assert w["c"].get(f"/api/canopy-sessions/attachments/{sent.pk}/content").status_code == 404
    # And a send there claims nothing, should one be pending.
    Attachment.objects.create(session=cold, uploaded_by=w["user"], filename="late.png",
                              content_type="image/png", size_bytes=3, storage_key="k/late")
    r = w["c"].post(f"/api/canopy-sessions/{cold.pk}/send", {"text": "look"},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    turn = Turn.objects.filter(chat_session=cold).latest("created_at")
    assert not (turn.origin_ref or {}).get("attachments")


# --- 7. a key copy is written only where there is none ---------------------------------------


def test_a_viewer_cannot_give_a_key_copy_and_nobody_overwrites_a_current_one(w):
    hot = w["hot"]
    viewer, vpriv, vc = _member(w, "viewer")
    _in_it(w, viewer, vpriv, hot, role=SessionParticipant.VIEWER)
    owner_copy = SessionKeyWrap.objects.get(session=hot, user=w["user"]).wrap
    junk = rewrap_for(hot, viewer, vpriv, w["user"])
    junk["ct"] = junk["ct"][:-4] + "AAAA"  # random bytes, the right shape
    r = vc.post(f"/api/canopy-sessions/{hot.pk}/key-copies", {"user_id": w["user"].pk, "wrap": junk},
                content_type="application/json")
    assert r.status_code == 403, r.content
    # An editor may give copies, but never over a current one — the owner's included.
    editor, epriv, ec_ = _member(w, "editor")
    _in_it(w, editor, epriv, hot)
    r = ec_.post(f"/api/canopy-sessions/{hot.pk}/key-copies", {"user_id": w["user"].pk, "wrap": junk},
                 content_type="application/json")
    assert r.status_code == 409, r.content
    assert SessionKeyWrap.objects.get(session=hot, user=w["user"]).wrap == owner_copy
    # Nor through adding them again (a role change keeps their copy).
    r = w["c"].post(f"/api/canopy-sessions/{hot.pk}/participants",
                    {"email": editor.email, "role": "viewer", "wrap": {**junk, "recipient_jkt": junk["recipient_jkt"]}},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    assert SessionKeyWrap.objects.get(session=hot, user=editor).recipient_jkt


def test_a_stale_copy_is_replaced_and_a_broken_one_is_set_aside_by_its_holder(w):
    hot = w["hot"]
    editor, epriv, ec_ = _member(w, "editor")
    _in_it(w, editor, epriv, hot)
    # Stale, provably: the owner's vault key is not the one their copy names.
    new = vault_key(w["user"])
    wrap = rewrap_for(hot, editor, epriv, w["user"])
    r = ec_.post(f"/api/canopy-sessions/{hot.pk}/key-copies", {"user_id": w["user"].pk, "wrap": wrap},
                 content_type="application/json")
    assert r.status_code == 200, r.content
    _converse(hot)
    detail = w["c"].get(f"/api/canopy-sessions/{hot.pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], new, w["user"].pk)
    # Broken, as only its holder knows: they set it aside, and then it can be given again.
    assert ec_.delete(f"/api/canopy-sessions/{hot.pk}/key-copies/mine").status_code == 200
    assert not SessionKeyWrap.objects.filter(session=hot, user=editor).exists()
    again = rewrap_for(hot, w["user"], new, editor)
    r = w["c"].post(f"/api/canopy-sessions/{hot.pk}/key-copies", {"user_id": editor.pk, "wrap": again},
                    content_type="application/json")
    assert r.status_code == 200, r.content


# --- 10. naming a private chat as `parent` is not a way to its key ---------------------------


def test_naming_a_private_chat_as_parent_copies_nothing_to_someone_not_in_it(w):
    hot = w["hot"]
    outsider, opriv, oc = _member(w, "outsider")
    r = oc.post("/api/canopy-sessions/", {"agent_slug": w["agent"].slug, "parent": {"session": str(hot.pk)}},
                content_type="application/json")
    assert r.status_code == 200, r.content
    child = Session.objects.get(pk=r.json()["id"])
    assert child.parent_session_id == hot.pk
    hot_key = SessionKey.objects.get(session=hot)
    child_key = SessionKey.objects.filter(session=child).first()
    assert child_key is None or child_key.conversation != hot_key.conversation
    assert not SessionKeyWrap.objects.filter(session=child, user=w["user"]).exists()
    assert oc.get("/api/canopy-sessions/my-keys").json() == [] or all(
        k["conversation"] != str(hot_key.conversation) for k in oc.get("/api/canopy-sessions/my-keys").json())
    assert not SessionParticipant.objects.filter(session=child, user=w["user"]).exists()


def test_one_of_its_people_carries_its_key_on(w):
    hot = w["hot"]
    r = w["c"].post("/api/canopy-sessions/", {"agent_slug": w["agent"].slug, "parent": {"session": str(hot.pk)}},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    child = Session.objects.get(pk=r.json()["id"])
    assert SessionKey.objects.get(session=child).conversation == SessionKey.objects.get(session=hot).conversation


# --- 11. the moment of the switch ------------------------------------------------------------


def test_a_keystroke_that_reaches_the_lock_after_the_switch_stores_nothing(w):
    """The keystroke path asks again under the session row's lock the switch takes: a
    keystroke whose first read predates the switch still stores nothing after it."""
    cold = w["cold"]
    draft = drafts.draft_for(cold, w["user"])
    real = drafts._sensitive
    calls = {"n": 0}

    def first_stale(session):
        calls["n"] += 1
        if calls["n"] == 1:
            services.add_runner_requirements(cold, {"sensitive_data"})  # the switch lands now
            return False
        return real(session)

    with mock.patch.object(drafts, "_sensitive", side_effect=first_stale):
        out = drafts.update_draft(cold, user=w["user"], expected_version=draft.version, body=ROW)
    assert out is None
    draft.refresh_from_db()
    assert draft.body == ""


def test_an_event_written_as_it_switches_is_stored_as_metadata(w):
    """`append_events` reads sensitivity under the turn's lock, from fresh rows: a turn
    object loaded before the switch still writes metadata after it."""
    cold = w["cold"]
    turn = _turn(cold, prompt=ASKED, status=Turn.RUNNING, claimed_by=w["runner"])
    loaded = Turn.objects.select_related("chat_session").get(pk=turn.pk)  # before the switch
    services.add_runner_requirements(cold, {"sensitive_data"})
    ledger.append_events(loaded, [{"kind": "assistant", "payload": {"text": REPLY}}])
    assert REPLY not in str(list(TurnEvent.objects.filter(turn=turn).values_list("payload", flat=True)))


def test_a_fork_made_while_it_was_ordinary_becomes_private_with_it(w):
    cold = w["cold"]
    _bound(w, cold)
    _converse(cold)
    r = w["c"].post(f"/api/canopy-sessions/{cold.pk}/fork", {"turn_index": 2 * 64},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    fork = Session.objects.get(pk=r.json()["id"])
    assert REPLY in str(list(Message.objects.filter(session=fork).values_list("plaintext", flat=True)))

    services.add_runner_requirements(cold, {"sensitive_data"})

    fork.refresh_from_db()
    assert sensitivity.is_sensitive(fork)
    stored = str(list(Message.objects.filter(session=fork).values("plaintext", "content")))
    assert REPLY not in stored and ASKED not in stored
    detail = w["c"].get(f"/api/canopy-sessions/{fork.pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)


def test_a_conversation_that_is_not_a_fork_is_left_as_it_is(w):
    child = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"],
                                   parent_session=w["cold"])
    services.add_runner_requirements(w["cold"], {"sensitive_data"})
    child.refresh_from_db()
    assert not sensitivity.is_sensitive(child)

