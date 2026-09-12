"""Moving a LIVE session between runners, history and all.

The regression these pin is not hypothetical and was not found by reading: on
2026-09-12 session 169212e2 was moved from `cloud-ec2-1` to `jj-mbp-cdp` by hand,
with `place` + `send` (the closest thing that existed). Execution moved correctly
— and every one of the session's 60+ pre-transfer Message rows was deleted the
moment the laptop shipped its first transcript, because a new box necessarily
opens a new claude session and `ensure_transcript_identity` reads a changed
`transcript_id` as "these rows are another conversation's". `has_more_before`
came back `false` with the oldest surviving row at index 448.

So the history test below is the point of the feature, not a detail of it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from canopy_transcript import BLOCK_STRIDE

pytestmark = pytest.mark.django_db

SESSION_CAPABLE = {"sessions": True, "projects": ["canopy-web"]}


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    cloud = Runner.objects.create(
        name="cloud-ec2-1", workspace=ws, kind=Runner.CLOUD, status=Runner.ONLINE,
        paired_by=user, host="cloud-ec2-1", capabilities=SESSION_CAPABLE,
    )
    laptop = Runner.objects.create(
        name="jj-mbp-cdp", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        paired_by=user, host="jjackson@mbp", capabilities=SESSION_CAPABLE,
    )
    c = Client()
    c.force_login(user)
    return user, ws, cloud, laptop, c


def _bound_session(ws, runner, *, indices=(0, 64, 128)):
    """A session live on `runner` with some transcript-ordinal history."""
    s = Session.objects.create(workspace=ws, project="canopy-web", title="widget design")
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key="a-task", emdash_project="canopy-web",
        host=runner.host, transcript_id="old-transcript-uuid", thread_key=str(s.id),
        pending_question={"id": "q1"}, agent_status="working", tail=[{"role": "user"}],
    )
    for i in indices:
        Message.objects.create(session=s, turn_index=i, role=Message.ASSISTANT,
                               plaintext=f"row {i}")
    return s


# --- the regression -------------------------------------------------------


def test_transfer_preserves_history_when_the_new_box_ships_a_new_transcript():
    """The 169212e2 failure, end to end.

    A transferred session's next ship carries a DIFFERENT transcript_id (the new
    box could not resume the old claude session). Before the epoch offset that
    dropped every row; now the inherited rows survive below the offset and the
    new box's rows land above it.
    """
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64, 128))
    chat.transfer_session(session=s, placement=str(laptop.id))

    offset = RunnerBinding.objects.get(session=s).index_offset
    # The new box ships ordinals that restart at 0 — head-on with the old rows.
    chat.ensure_transcript_identity(s, "brand-new-transcript-uuid")
    chat.persist_transcript_rows(s, [
        {"index": 0, "role": Message.USER, "text": "picking this up"},
        {"index": 64, "role": Message.ASSISTANT, "text": "verified both PRs"},
    ])

    held = sorted(Message.objects.filter(session=s).values_list("turn_index", flat=True))
    assert held == [0, 64, 128, offset, offset + 64], (
        "inherited history must survive a post-transfer transcript change; "
        "new rows must land above it"
    )
    assert Message.objects.get(session=s, turn_index=0).plaintext == "row 0"
    assert Message.objects.get(session=s, turn_index=offset).plaintext == "picking this up"


def test_a_later_transcript_change_still_drops_only_the_current_epoch():
    """Transfer does not disable #615's protection, it scopes it.

    Reusing a task name ON the new box must still drop that box's rows — just not
    the ones carried across.
    """
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64))
    chat.transfer_session(session=s, placement=str(laptop.id))
    offset = RunnerBinding.objects.get(session=s).index_offset

    chat.ensure_transcript_identity(s, "transcript-A")
    chat.persist_transcript_rows(s, [{"index": 0, "role": Message.USER, "text": "A"}])
    # A genuinely different conversation appears under the same task name.
    chat.ensure_transcript_identity(s, "transcript-B")

    held = sorted(Message.objects.filter(session=s).values_list("turn_index", flat=True))
    assert held == [0, 64], "epoch rows go; inherited rows stay"
    assert offset not in held


def test_an_ordinal_scheme_upgrade_also_spares_the_inherited_epoch():
    """The OTHER wipe path, and the one that nearly slipped through.

    `_ensure_current_ordinal_scheme` drops on a scheme change with the same
    "drop and re-derive" reasoning — which is unavailable across a transfer,
    because the transcript those rows came from is on a box we may never reach
    again. So the scoping has to apply to both paths, not just the one this
    feature was written for.
    """
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64))
    Session.objects.filter(pk=s.pk).update(ordinal_scheme=0)  # a legacy session
    s.refresh_from_db()
    chat.transfer_session(session=s, placement=str(laptop.id))
    offset = RunnerBinding.objects.get(session=s).index_offset

    chat.persist_transcript_rows(s, [{"index": 0, "role": Message.USER, "text": "new"}])

    held = sorted(Message.objects.filter(session=s).values_list("turn_index", flat=True))
    assert held == [0, 64, offset], "a scheme upgrade must not take the inherited epoch"
    s.refresh_from_db()
    assert s.ordinal_scheme != 0, "the session is still moved onto the current scheme"


def test_untransferred_session_keeps_the_old_drop_everything_behaviour():
    """Offset 0 must be byte-for-byte the previous semantics — the overwhelming
    majority of sessions, and the case #615 was actually about."""
    _u, ws, cloud, _laptop, _c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64, 128))
    assert RunnerBinding.objects.get(session=s).index_offset == 0
    chat.ensure_transcript_identity(s, "a-different-transcript")
    assert Message.objects.filter(session=s).count() == 0


# --- the move itself ------------------------------------------------------


def test_transfer_repoints_the_binding_and_clears_the_old_box_state():
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    binding, _turn = chat.transfer_session(session=s, placement=str(laptop.id))

    binding.refresh_from_db()
    assert binding.runner_id == laptop.id
    assert binding.transferred_from_id == cloud.id
    assert binding.transferred_at is not None
    # session_key + host are what `reusable_by` consults; left set they would send
    # the laptop looking for a task that only exists on the cloud box.
    assert binding.session_key == ""
    assert binding.host == ""
    assert binding.transcript_id == ""
    assert binding.pending_question is None
    assert binding.agent_status == ""
    assert binding.tail == []
    assert not binding.reusable_by(laptop), "the target must open a FRESH session"


def test_transfer_offset_clears_the_high_water_mark_on_a_stride_boundary():
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64, 130))
    binding, _turn = chat.transfer_session(session=s, placement=str(laptop.id))
    assert binding.index_offset % BLOCK_STRIDE == 0
    assert binding.index_offset > 130


def test_transfer_enqueues_a_turn_pinned_to_the_target_carrying_the_brief():
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    _binding, turn = chat.transfer_session(
        session=s, placement=str(laptop.id), brief="BRANCH: docs/widget-design",
    )
    assert turn.pinned_runner_id == laptop.id
    assert turn.chat_session_id == s.id
    assert turn.status == Turn.QUEUED
    # The preamble is server-side so no caller can ship a transfer without it.
    assert "transferred from cloud-ec2-1 to jj-mbp-cdp" in turn.prompt
    assert "FRESH session" in turn.prompt
    assert "BRANCH: docs/widget-design" in turn.prompt


def test_transfer_re_pins_later_unrouted_sends_to_the_target():
    """Without this a send that arrives before the target's first report falls to
    open routing and can land back on the box we just left."""
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    chat.transfer_session(session=s, placement=str(laptop.id))
    s.refresh_from_db()
    assert s.metadata["requested_runner_id"] == str(laptop.id)


def test_transferring_back_does_not_dedupe_onto_the_outbound_turn():
    """Both directions in one day is the NORMAL shape of a failover (ada's
    user-switch measured it on 2026-07-31), so the idempotency key must not
    collapse the return leg onto the outbound one."""
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    _b, out = chat.transfer_session(session=s, placement=str(laptop.id))
    _b2, back = chat.transfer_session(session=s, placement=str(cloud.id))
    assert out.id != back.id
    assert out.idempotency_key != back.idempotency_key


# --- refusals -------------------------------------------------------------


def test_transfer_refuses_an_unknown_or_non_session_capable_runner():
    user, ws, cloud, _laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    no_sessions = Runner.objects.create(
        name="build-box", workspace=ws, status=Runner.ONLINE, paired_by=user,
        capabilities={"projects": ["canopy-web"]},  # sessions: absent
    )
    with pytest.raises(ValueError):
        chat.transfer_session(session=s, placement=str(no_sessions.id))
    with pytest.raises(ValueError):
        chat.transfer_session(session=s, placement="not-a-uuid")


def test_transfer_refuses_a_no_op_move_to_the_same_runner():
    _u, ws, cloud, _laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    with pytest.raises(ValueError, match="already on"):
        chat.transfer_session(session=s, placement=str(cloud.id))


def test_transfer_refuses_while_a_turn_is_executing():
    """A box mid-thought keeps writing into the epoch we are about to close."""
    _u, ws, cloud, laptop, _c = _ctx()
    s = _bound_session(ws, cloud)
    Turn.objects.create(
        chat_session=s, workspace=ws, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key="k1", status=Turn.RUNNING, claimed_by=cloud,
    )
    with pytest.raises(RuntimeError, match="still executing"):
        chat.transfer_session(session=s, placement=str(laptop.id))


def test_transfer_refuses_a_session_with_no_binding():
    _u, ws, _cloud, laptop, _c = _ctx()
    s = Session.objects.create(workspace=ws, project="canopy-web", title="never ran")
    with pytest.raises(LookupError):
        chat.transfer_session(session=s, placement=str(laptop.id))


# --- the descriptor the runner reads --------------------------------------


def test_streams_report_markers_in_the_runners_own_ordinal_space():
    """The runner compares these against ordinals from its OWN file, so the
    offset must be undone on the way out. Unfiltered, the inherited high-water
    mark would be unreachable and the live conversation would never ship — #615's
    failure, reintroduced by preserving history."""
    _u, ws, cloud, laptop, c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64, 128))
    chat.transfer_session(session=s, placement=str(laptop.id))
    binding = RunnerBinding.objects.get(session=s)
    binding.session_key = "fresh-local-task"  # the target reported in
    binding.save(update_fields=["session_key"])

    body = c.get(f"/api/harness/runners/{laptop.id}/streams").json()
    row = next(x for x in body["streams"] if x["session_id"] == str(s.id))
    # Nothing of THIS transcript is held yet: ship from the top.
    assert row["first_index"] is None
    assert row["last_index"] is None

    chat.persist_transcript_rows(s, [
        {"index": 0, "role": Message.USER, "text": "hi"},
        {"index": 192, "role": Message.ASSISTANT, "text": "there"},
    ])
    row = next(
        x for x in c.get(f"/api/harness/runners/{laptop.id}/streams").json()["streams"]
        if x["session_id"] == str(s.id)
    )
    assert (row["first_index"], row["last_index"]) == (0, 192), (
        "markers are the runner's ordinals, not the server's shifted ones"
    )


def test_streams_markers_are_unchanged_for_a_never_transferred_session():
    _u, ws, cloud, _laptop, c = _ctx()
    s = _bound_session(ws, cloud, indices=(0, 64, 128))
    body = c.get(f"/api/harness/runners/{cloud.id}/streams").json()
    row = next(x for x in body["streams"] if x["session_id"] == str(s.id))
    assert (row["first_index"], row["last_index"]) == (0, 128)


# --- the endpoint ---------------------------------------------------------


def test_transfer_endpoint_moves_the_session_and_reports_the_epoch():
    _u, ws, cloud, laptop, c = _ctx()
    s = _bound_session(ws, cloud)
    r = c.post(
        f"/api/canopy-sessions/{s.id}/transfer",
        data={"runner": str(laptop.id), "brief": "BRANCH: x"},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["runner"] == "jj-mbp-cdp"
    assert body["transferred_from"] == "cloud-ec2-1"
    assert body["index_offset"] > 0
    assert Turn.objects.get(id=body["turn_id"]).pinned_runner_id == laptop.id


def test_transfer_endpoint_409s_while_a_turn_executes():
    """409, not 422: the body is fine and the request will succeed once the
    source box is idle. A 422 would send the caller looking at their payload."""
    _u, ws, cloud, laptop, c = _ctx()
    s = _bound_session(ws, cloud)
    Turn.objects.create(
        chat_session=s, workspace=ws, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key="k1", status=Turn.RUNNING, claimed_by=cloud,
    )
    r = c.post(
        f"/api/canopy-sessions/{s.id}/transfer",
        data={"runner": str(laptop.id)}, content_type="application/json",
    )
    assert r.status_code == 409, r.content


def test_transfer_endpoint_422s_on_an_unknown_runner():
    _u, ws, cloud, _laptop, c = _ctx()
    s = _bound_session(ws, cloud)
    r = c.post(
        f"/api/canopy-sessions/{s.id}/transfer",
        data={"runner": "6f1c9e2a-0000-4000-8000-000000000000"},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content
