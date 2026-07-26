"""One way to record a chat, whatever surface it started on.

Where a conversation ORIGINATED (a phone composer vs a task discovered in emdash)
said nothing about where its record should live — but it used to decide it. A
phone-created chat recorded only what happened inside a Turn, so anything outside
one was lost forever: text you typed straight into emdash, and text the agent
wrote after handing the floor back (a background job finishing). Both sit in the
same emdash session, in the same transcript, as everything else.

`services.transcript_sourced` replaces the origin test: a session driven by a real
runner takes its durable rows from the transcript, keyed on record ordinals,
whoever started it. The ledger projection survives only where there IS no
transcript — the dev stub.
"""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness import services as harness_services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=user,
        host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    return user, ws, runner


def test_a_phone_created_chat_is_transcript_sourced_when_a_runner_will_drive_it(settings):
    settings.CHAT_STUB_EXECUTOR = False
    user, ws, _runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    assert session.origin == Session.ORIGIN_WEB      # started on the phone...
    assert services.transcript_sourced(session)      # ...recorded like any other


def test_the_dev_stub_keeps_the_ledger_as_its_source(settings):
    """There is no emdash session and no transcript to read, so the projection
    stays — the fallback is about the absence of a transcript, not about origin."""
    settings.CHAT_STUB_EXECUTOR = True
    user, ws, _runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    assert not services.transcript_sourced(session)


def test_a_discovered_session_is_transcript_sourced_without_a_flag(settings):
    settings.CHAT_STUB_EXECUTOR = True   # irrelevant: it already exists in emdash
    user, ws, _runner = _ctx()
    session = Session.objects.create(
        workspace=ws, origin=Session.ORIGIN_RUNNER, project="canopy-web", title="t",
    )
    assert services.transcript_sourced(session)


def test_sessions_created_before_the_unification_stay_ledger_sourced(settings):
    """A pre-existing chat's rows are numbered by a dense counter (0,1,2…), which
    would collide with transcript ordinals in the same column — so an unflagged
    web session keeps the old path for life rather than being switched mid-history."""
    settings.CHAT_STUB_EXECUTOR = False
    user, ws, _runner = _ctx()
    legacy = Session.objects.create(
        workspace=ws, origin=Session.ORIGIN_WEB, project="canopy-web", metadata={},
    )
    assert not services.transcript_sourced(legacy)


def test_the_agent_speaking_after_its_turn_ends_still_reaches_a_phone_chat(settings):
    """THE BUG this unification fixes. The agent ends its turn ("I'll report once
    it finishes"), the turn closes, and 40s later the background job completes and
    it speaks again — outside any turn. Those records reach the session because the
    transcript, not the turn, is what's being recorded."""
    settings.CHAT_STUB_EXECUTOR = False
    user, ws, runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    RunnerBinding.objects.create(
        session=session, runner=runner, session_key="canopy-web-1", host="jj@mbp",
    )
    # What the runner ships from the transcript after the turn is over.
    services.persist_transcript_rows(session, [
        {"index": 36, "role": "assistant", "text": "The background job finished."},
        {"index": 38, "role": "assistant", "text": "It printed SENTINEL-READY."},
    ])
    assert [(m.turn_index, m.plaintext) for m in session.messages.order_by("turn_index")] == [
        (36, "The background job finished."),
        (38, "It printed SENTINEL-READY."),
    ]


def test_a_turns_events_are_not_projected_for_a_transcript_sourced_chat(settings):
    """The other half of "one way": the bridge still STREAMS the reply live, but it
    no longer also writes rows — that second copy at a different index is exactly
    the collision that forced the two paths apart."""
    settings.CHAT_STUB_EXECUTOR = False
    user, ws, runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    harness_services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hello"}}])
    # Called directly: the signal that normally drives it fires post-commit, which
    # a test transaction never reaches.
    written = services.project_events(turn, list(turn.events.all()))
    assert written == 0
    assert session.messages.count() == 0, "the transcript is the record, not the ledger"
    assert turn.events.filter(kind="assistant").exists(), "but it still streams live"


def test_the_stub_path_still_projects_its_ledger(settings):
    settings.CHAT_STUB_EXECUTOR = True
    user, ws, _runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    harness_services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hello"}}])
    services.project_events(turn, list(turn.events.all()))
    roles = [m.role for m in session.messages.order_by("turn_index")]
    assert roles == [Message.USER, Message.ASSISTANT]


def test_a_transcript_sourced_send_writes_no_row_but_keeps_the_text(settings):
    settings.CHAT_STUB_EXECUTOR = False
    user, ws, _runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    msg, turn = services.send_message(session=session, text="what changed?", user=user)
    assert session.messages.count() == 0
    assert msg.plaintext == "what changed?"        # echoed to the sender
    assert turn.prompt == "what changed?"          # and recoverable while it waits
    assert turn.status == Turn.QUEUED


def test_convert_command_refuses_a_session_with_no_transcript(settings):
    """Refuse rather than delete rows we could not replace."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.CHAT_STUB_EXECUTOR = True
    user, ws, _runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    services.project_events(turn, list(turn.events.all()))
    before = session.messages.count()
    with pytest.raises(CommandError, match="no runner binding"):
        call_command("convert_chat_to_transcript", str(session.id))
    assert session.messages.count() == before, "nothing was deleted"


def test_convert_command_swaps_a_legacy_chat_onto_its_transcript(settings):
    from django.core.management import call_command

    settings.CHAT_STUB_EXECUTOR = True          # created legacy (ledger-sourced)
    user, ws, runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    services.project_events(turn, list(turn.events.all()))
    assert session.messages.count() > 0
    RunnerBinding.objects.create(
        session=session, runner=runner, session_key="canopy-web-1", host="jj@mbp",
    )
    call_command("convert_chat_to_transcript", str(session.id))
    session.refresh_from_db()
    assert services.transcript_sourced(session)
    assert session.messages.count() == 0, "dense rows dropped; the transcript replaces them"
    binding = RunnerBinding.objects.get(session=session)
    assert binding.backfill_requested is True


def test_convert_command_dry_run_changes_nothing(settings):
    from django.core.management import call_command

    settings.CHAT_STUB_EXECUTOR = True
    user, ws, runner = _ctx()
    session = services.create_session(workspace=ws, created_by=user, project="canopy-web")
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    services.project_events(turn, list(turn.events.all()))
    RunnerBinding.objects.create(
        session=session, runner=runner, session_key="canopy-web-1", host="jj@mbp",
    )
    before = session.messages.count()
    call_command("convert_chat_to_transcript", str(session.id), "--dry-run")
    session.refresh_from_db()
    assert session.messages.count() == before
    assert not services.transcript_sourced(session)
