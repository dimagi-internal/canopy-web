"""A session's rows belong to ONE transcript — the guard for issue #615.

A `RunnerBinding` is keyed on the emdash task NAME, and names are reused: close a
task called "bednet", open another, and the binding is silently re-pointed at a
new conversation with the previous one's Message rows still attached. That alone
renders one session as another. What made it unrecoverable is that `turn_index` is
a PER-FILE ordinal, so the first_index/last_index markers the server derives from
the old rows are meaningless against the new file — measured on prod 2026-08-14, a
593-record predecessor left last_index=37,696 against a live 384-record transcript
whose highest possible ordinal was 24,575, so every record of the live session sat
below the marker and shipped nothing for 23.5 hours.

`ensure_transcript_identity` is the sibling of `_ensure_current_ordinal_scheme`:
the other way a session's ordinal space is invalidated. A changed transcript opens
a new EPOCH above the held rows rather than dropping them (issue #1731: a
credential failover moved a live session to a fresh claude conversation and the
drop deleted history nothing could re-derive). Name reuse — the case dropping was
built for — is caught earlier by the forks. Only rows of unknown provenance (a
blank recorded id) are still dropped and re-derived.
"""
from __future__ import annotations

import pytest
from canopy_transcript import BLOCK_STRIDE
from django.contrib.auth.models import User

from apps.canopy_sessions import transcript_ingest
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _ctx(transcript_id=""):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    runner = Runner.objects.create(
        name="mbp", kind=Runner.EMDASH, owner=user, host="mbp.local", capabilities={},
    )
    session = Session.objects.create(
        workspace=ws, origin=Session.ORIGIN_RUNNER, title="bednet", project="ace",
    )
    binding = RunnerBinding.objects.create(
        session=session, runner=runner, session_key="bednet", host="mbp.local",
        transcript_id=transcript_id,
    )
    return session, binding


def _rows(session, indices, prefix="old"):
    transcript_ingest.persist_transcript_rows(
        session, [{"index": i, "role": "assistant", "text": f"{prefix} {i}"} for i in indices]
    )


def test_a_different_transcript_keeps_the_held_rows_and_opens_an_epoch():
    """Issue #1731: same session, new claude conversation (a credential failover
    that could not resume). The new file holds none of the old history, so the
    old rows are the only copy left — they stay, and the new file lands above."""
    session, binding = _ctx(transcript_id="fc41aa1a")
    _rows(session, [64, 128, 37696])

    assert transcript_ingest.ensure_transcript_identity(session, "737c1a0a") == 0

    assert Message.objects.filter(session=session).count() == 3
    binding.refresh_from_db()
    assert binding.transcript_id == "737c1a0a"
    assert binding.index_offset > 37696
    assert binding.index_offset % BLOCK_STRIDE == 0


def test_the_new_transcript_lands_above_the_old_one_without_colliding():
    """The #615 hazard the drop used to prevent: the new file's ordinals restart
    near 0 and would collide with, or sort among, the old ones. The epoch shifts
    them past the high-water mark instead, so the render stays chronological."""
    session, binding = _ctx(transcript_id="fc41aa1a")
    _rows(session, [64, 128, 37696])
    transcript_ingest.ensure_transcript_identity(session, "737c1a0a")

    _rows(session, [64, 128], prefix="new")

    texts = list(
        Message.objects.filter(session=session).order_by("turn_index")
        .values_list("plaintext", flat=True)
    )
    assert texts == ["old 64", "old 128", "old 37696", "new 64", "new 128"]


def test_a_second_change_opens_another_epoch_above_both():
    """Epochs stack: each change keeps everything below it."""
    session, binding = _ctx(transcript_id="a")
    _rows(session, [64])
    transcript_ingest.ensure_transcript_identity(session, "b")
    _rows(session, [64], prefix="b")
    first = RunnerBinding.objects.get(pk=binding.pk).index_offset

    transcript_ingest.ensure_transcript_identity(session, "c")
    _rows(session, [64], prefix="c")

    assert RunnerBinding.objects.get(pk=binding.pk).index_offset > first
    assert Message.objects.filter(session=session).count() == 3


def test_the_same_transcript_keeps_everything():
    """A continuation is the overwhelmingly common case and must be free."""
    session, binding = _ctx(transcript_id="b8c44ef3")
    _rows(session, [64, 128])

    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 0
    assert Message.objects.filter(session=session).count() == 2


def test_an_old_runner_making_no_claim_changes_nothing():
    """A blank id is not evidence of anything. A runner that predates the field
    must never have a healthy session wiped out from under it."""
    session, _ = _ctx(transcript_id="b8c44ef3")
    _rows(session, [64, 128])

    assert transcript_ingest.ensure_transcript_identity(session, "") == 0
    assert Message.objects.filter(session=session).count() == 2


def test_rows_of_unknown_provenance_are_rebuilt_once():
    """A session whose rows predate the field: they may well be a previous task's
    (that IS the reported bug), and the runner ships the full history precisely
    because the server named no transcript. Rebuilding is what makes the fix
    self-healing on deploy rather than a hand-run reset per session."""
    session, binding = _ctx(transcript_id="")
    _rows(session, [64, 128])

    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 2
    binding.refresh_from_db()
    assert binding.transcript_id == "b8c44ef3"
    # ...and only once: the second ship is a plain continuation.
    _rows(session, [192], prefix="new")
    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 0
    assert Message.objects.filter(session=session).count() == 1


def test_a_chunked_ship_cannot_delete_its_own_earlier_chunks():
    """A transcript is shipped in byte-budgeted chunks, every one of them naming
    the same transcript. Only the first can drop; if a later chunk could, a long
    history would arrive and destroy itself one chunk at a time."""
    session, _ = _ctx(transcript_id="")
    _rows(session, [64])

    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 1   # chunk 1
    _rows(session, [64, 128], prefix="new")
    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 0   # chunk 2
    _rows(session, [192], prefix="new")
    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 0   # chunk 3
    assert Message.objects.filter(session=session).count() == 3


def test_a_chunked_ship_opens_its_epoch_only_once():
    """Same idempotence for a changed id: chunk 1 opens the epoch and records the
    id; later chunks match and must not lift the offset again, or each chunk
    would land in an epoch of its own."""
    session, binding = _ctx(transcript_id="6ee00fa8")
    _rows(session, [64])

    transcript_ingest.ensure_transcript_identity(session, "b8c44ef3")                # chunk 1
    offset = RunnerBinding.objects.get(pk=binding.pk).index_offset
    _rows(session, [64, 128], prefix="new")
    transcript_ingest.ensure_transcript_identity(session, "b8c44ef3")                # chunk 2

    assert RunnerBinding.objects.get(pk=binding.pk).index_offset == offset
    assert Message.objects.filter(session=session).count() == 3


def test_an_unbound_session_is_left_alone():
    """No binding means no provenance to compare against — and a web chat's rows
    are not derived from a transcript at all."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="chat")
    _rows(session, [64])

    assert transcript_ingest.ensure_transcript_identity(session, "b8c44ef3") == 0
    assert Message.objects.filter(session=session).count() == 1
