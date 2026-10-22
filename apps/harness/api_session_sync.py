"""/api/harness runner <-> chat-session sync: resolving and recording a turn's
session, the runner's session reports, stream/backfill/close/menu-answer sync,
admission notices and session backfills.

Registered on the harness router (`api.router`), in the order api.py imports
this module, so the schema and the MCP tool names are unchanged."""
from __future__ import annotations

import logging
import uuid

from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja.errors import HttpError

from apps.agents.gates import agent_for_read
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import session_sync

# The one harness router and the helpers every harness route shares; api.py
# imports this module after defining them.
from .api import (  # noqa: E402
    router,
    runner_or_404,
)
from .models import (
    Runner,
    Turn,
)
from .schemas import (
    AdmissionNoticeClaimIn,
    AdmissionNoticeClaimOut,
    AdmissionNoticeReportIn,
    BackfillSyncOut,
    BackfillWriteOut,
    CloseResultsIn,
    CloseResultsOut,
    CloseSyncOut,
    MenuAnswerResultIn,
    MenuAnswerResultOut,
    MenuAnswerSyncOut,
    RecordSessionIn,
    ReportSessionsIn,
    ResolveSessionIn,
    ResolveSessionOut,
    SessionBackfillIn,
    SessionMenuIn,
    SessionMenuOut,
    SessionReportOut,
    SessionStreamIn,
    StreamPostOut,
    StreamSyncOut,
)

logger = logging.getLogger(__name__)

#: Published operationIds keep the label of the module these routes moved out of
#: (apps/api/api.py `_app_label`): an operationId is an MCP tool name.
OPERATION_LABEL = "harness"


def _project_workspace_or_404(request: HttpRequest, ws_slug: str):
    """Tenant-gate a project session's workspace, mirroring agent_for_read for the
    agent case. A project link has no agent to derive tenancy from, so without
    this any runner could read another user's rolling `summary` by guessing
    thread_key.

    The workspace is passed EXPLICITLY (from the turn the runner is executing, via
    TurnOut.workspace_slug), not derived from a default: the owner may belong to
    several workspaces, and a project turn already carries the one it belongs to.
    The owner must be a member of it. Same 404-not-403 rule: a non-member gets
    404, never a disclosure that the workspace exists.
    """
    if not ws_slug or not wsvc.is_member(request.user, ws_slug):
        raise HttpError(404, "workspace not found")
    ws = Workspace.objects.filter(slug=ws_slug).first()
    if ws is None:
        raise HttpError(404, "workspace not found")
    return ws


@router.post("/runners/{runner_id}/resolve-session", response=ResolveSessionOut)
def resolve_session(request: HttpRequest, runner_id: uuid.UUID, payload: ResolveSessionIn):
    """Given (target, thread_key), tell THIS runner whether it can reuse an existing
    emdash session (it owns the live hint) or must spawn fresh + rehydrate context.
    Runner-scoped because reuse depends on the caller's macOS host."""
    runner = runner_or_404(request, runner_id)
    if payload.project:
        ws = _project_workspace_or_404(request, payload.workspace)
        return session_sync.resolve_session(
            None, payload.thread_key, runner, project=payload.project, workspace=ws,
            turn_id=payload.turn_id,
        )
    agent = agent_for_read(request, payload.agent_slug)
    return session_sync.resolve_session(agent, payload.thread_key, runner, turn_id=payload.turn_id)


@router.post("/runners/{runner_id}/record-session", response=ResolveSessionOut)
def record_session(request: HttpRequest, runner_id: uuid.UUID, payload: RecordSessionIn):
    """Upsert the durable link and point its live-session hint at THIS runner/host,
    after a session was created or reused for the thread. Returns the fresh resolution."""
    runner = runner_or_404(request, runner_id)
    try:
        return _record_session(request, runner, payload)
    except HttpError:
        raise
    except Exception:
        # Failure investigation 3 (2026-10-15, turn 282f825e): a transferred chat
        # turn (ace, haldimagi-mbp-cdp) 500'd here on all four attempts, and the
        # response is the generic problem body with nothing to tell which line
        # raised. Name the inputs that pick the branch, then re-raise unchanged.
        logger.exception(
            "record-session failed: runner=%s agent=%s project=%r thread_key=%r "
            "session_key=%r turn_id=%s",
            runner.pk, payload.agent_slug, payload.project, payload.thread_key,
            payload.session_key, payload.turn_id,
        )
        raise


def _record_session(request: HttpRequest, runner: Runner, payload: RecordSessionIn):
    # The turn this session is being made for, when the runner names one it holds:
    # a new session takes its title and its parent from it.
    turn = None
    if payload.turn_id:
        turn = (Turn.objects.select_related("agent", "chat_session__agent")
                .filter(pk=payload.turn_id, claimed_by=runner).first())
    if payload.project:
        ws = _project_workspace_or_404(request, payload.workspace)
        try:
            session_sync.record_session(
                None, payload.thread_key, runner=runner, project=payload.project, workspace=ws,
                session_key=payload.session_key, session_id=payload.session_id,
                agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
                title=payload.title, turn=turn,
            )
        except session_sync.ThreadSessionNotFound:
            raise HttpError(404, "session not found")
        return session_sync.resolve_session(
            None, payload.thread_key, runner, project=payload.project, workspace=ws
        )
    agent = agent_for_read(request, payload.agent_slug)
    title = payload.title
    if turn is not None:
        # The server names the session from the turn when it knows it: the email's
        # subject, the schedule's name — never the raw command a cloud turn's
        # prompt often is ("/ace:turn --thread …", 2026-10-03).
        title = session_sync.turn_session_title(turn, payload.title)
    try:
        session_sync.record_session(
            agent, payload.thread_key, runner=runner,
            session_key=payload.session_key, session_id=payload.session_id,
            agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
            title=title, turn=turn,
        )
    except session_sync.ThreadSessionNotFound:
        # A session id that is not this agent's, or sits in a tenant the runner's
        # owner is not in: refused as if it did not exist, and nothing rebound.
        raise HttpError(404, "session not found")
    if payload.turn_id and payload.session_key:
        session_sync.stamp_turn_session(payload.turn_id, runner, payload.session_key)
    return session_sync.resolve_session(agent, payload.thread_key, runner)


@router.post("/runners/{runner_id}/sessions", response=SessionReportOut)
def report_sessions(request: HttpRequest, runner_id: uuid.UUID, payload: ReportSessionsIn):
    """The runner reports the open emdash sessions it can see. Wholesale per runner.
    Owner-gated via _runner_or_404 (404, not 403). Sessions are tenant-owned: a NEW
    session follows its project's agent when the project names one, else the
    runner owner's default session workspace (`/api/me/session-workspace/`), else
    the runner's workspace. An existing session is never re-filed."""
    runner = runner_or_404(request, runner_id)
    ws = runner.workspace
    if ws is None:
        raise HttpError(404, "runner has no workspace")
    skipped: list = []
    count = session_sync.replace_reported_sessions(
        runner, ws, payload.sessions, payload.archived, complete=payload.complete,
        skipped=skipped,
    )
    return SessionReportOut(count=count, skipped=len(skipped))


@router.get("/runners/{runner_id}/streams", response=StreamSyncOut)
def list_streams(request: HttpRequest, runner_id: uuid.UUID):
    """Every session this runner backs, with what the server already holds of each.

    NOT just the watched ones any more, and that is the point. While this filtered
    on `stream_desired=True`, transcript rows only ever reached the server for a
    session someone happened to have open, and only from the moment they opened it
    — so the durable record was a side effect of being looked at. Measured on labs
    (2026-07-31) across 12 live sessions: the server held 983 of 6119 rows (16%),
    with 8 sessions at exactly zero. "Load full session" existed to paper over that
    by asking the runner at read time, which is why it was both slow and unreliable.

    `live` is the old `stream_desired` — it now decides only whether rows FAN OUT
    to watching clients, never whether they are persisted. Persisting is
    unconditional because the transcript is the durable source (spec 2026-07-24)
    and building the rows costs the runner ~29 ms for a 6.5 MB file; there was
    never a cost argument for keeping it, only the accident that live-view and
    durability shared one flag.

    `first_index`/`last_index` bound what the server holds, so the runner can tell
    "ship only what is new" from "ship the history I am missing" — a Max alone can
    only ever append above the high-water mark and so can never fill a hole below
    it, which is how a session ended up stuck at 8.6% with no way to self-heal."""
    from django.db.models import F
    from django.db.models import Max as _Max
    from django.db.models import Min as _Min

    runner = runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, session__status=Session.ACTIVE)
        .exclude(session_key="")
        # ACTIVE only. Widening this from `stream_desired` to "every session"
        # would otherwise sweep in every session the box has ever held — labs
        # accumulated 71 at one point — and re-ship each one's full history on
        # every runner restart, for conversations that are retired and whose
        # transcripts are not growing. An archived session is not abandoned,
        # though: `drain_backfills` is a separate path this filter does not
        # touch, so an explicit "Load full session" on one still works. Eager for
        # live sessions, on demand for retired ones.
        # Scoped to the CURRENT EPOCH, and reported in the RUNNER's own ordinal
        # space (see RunnerBinding.index_offset). Both halves matter for a
        # transferred session, and for opposite reasons:
        #   - the filter, because rows inherited from the previous box are not in
        #     this runner's transcript at all. An unfiltered Min/Max would hand it
        #     a high-water mark it can never reach, so every record of the live
        #     conversation would sit BELOW the marker and never ship — issue #615's
        #     failure exactly, reintroduced by preserving history rather than by
        #     reusing a task name.
        #   - the subtraction, because the runner compares these against ordinals
        #     it computes from its OWN file. It needs no knowledge of the offset:
        #     the shift is applied on write (persist_transcript_rows) and undone
        #     here, so both directions stay inside the server.
        # Offset 0 (never transferred) makes the filter cover everything and the
        # subtraction a no-op, i.e. the previous behaviour byte for byte.
        .annotate(
            _first_index=_Min(
                "session__messages__turn_index",
                filter=Q(session__messages__turn_index__gte=F("index_offset")),
            ),
            _last_index=_Max(
                "session__messages__turn_index",
                filter=Q(session__messages__turn_index__gte=F("index_offset")),
            ),
        )
    )

    def _local(marker, offset):
        """A server-space marker in the runner's own ordinal space. None stays
        None — "I hold nothing of this transcript", which is what a freshly
        transferred session is, and it tells the runner to ship from the top."""
        return None if marker is None else max(int(marker) - int(offset or 0), 0)

    return {"streams": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         # emdash_project, never `project`: an agent chat leaves `project` blank
         # and its worktree lives under the agent's own repo. Sending "" here is
         # what stopped agent sessions ever being streamed or backfilled.
         "project": b.session.emdash_project,
         "last_index": _local(b._last_index, b.index_offset),
         "first_index": _local(b._first_index, b.index_offset),
         "live": b.stream_desired,
         # What the markers above are markers INTO. They are per-file ordinals, so
         # a runner reading a different transcript must discard them rather than
         # resume against them (issue #615).
         "transcript_id": b.transcript_id}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/session-stream", response=StreamPostOut)
def post_session_stream(request: HttpRequest, runner_id: uuid.UUID, payload: SessionStreamIn):
    """The runner ships live conversational events for a session it backs. For an
    origin=runner session, events carrying a transcript ordinal are PERSISTED as
    Message rows first (the transcript is the durable source — spec 2026-07-24),
    then the assistant frames fan out to the session group as the same
    chat.turn_event frames the chat path uses (turn-less -> the consumer derives
    seq:<n> message ids). User events are persisted but never live-pushed — the
    sender's client already echoed them optimistically."""
    runner = runner_or_404(request, runner_id)
    binding = (
        RunnerBinding.objects.select_related("session")
        .filter(session_id=payload.session_id, runner=runner).first()
    )
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    return {"count": session_sync.ingest_session_stream(
        binding, transcript_id=payload.transcript_id, events=payload.events)}


@router.get("/runners/{runner_id}/backfills", response=BackfillSyncOut)
def list_backfills(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions this runner has been asked to ship full history for."""
    runner = runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, backfill_requested=True)
    )
    return {"backfills": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.emdash_project}
        for b in bindings
    ]}


@router.get("/runners/{runner_id}/closes", response=CloseSyncOut)
def list_closes(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions this runner has been asked to close and has not closed yet.

    The twin of `/menu-answers`: `close_session` relays a WS frame, and a frame
    published while the control channel is down is discarded silently while the
    API answers `ok:true`. Drained on the poll tick, so a lost frame costs a tick
    rather than leaving the emdash task open and the session active forever.
    """
    runner = runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, close_requested=True)
        .exclude(session_key="")
    )
    return {"closes": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         # The binding's own copy first: it survives the agent being deleted,
         # which blanks the session's (derived from the agent's slug).
         "project": b.emdash_project or b.session.emdash_project, "close_error": b.close_error}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/close-results", response=CloseResultsOut)
def post_close_results(request: HttpRequest, runner_id: uuid.UUID, payload: CloseResultsIn):
    """The runner reports what became of its attempts to close sessions.

    A failure is recorded on the session (`close_error`) so Supervisor can say
    "Close failed: <reason>" and offer a Retry; a success clears it. Every close
    path reports here, including the menu bar's "Close sessions", which has no
    server-side request behind it. Matched on (this runner, project, task name);
    a result for a task the server does not know is ignored.
    """
    runner = runner_or_404(request, runner_id)
    now = timezone.now()
    updated = 0
    for r in payload.results:
        if not r.session_key:
            continue
        # The project is half the key (task names repeat across projects). A
        # binding written before the project was recorded carries a blank, so a
        # named project also matches a blank one — never a DIFFERENT project.
        where = Q(emdash_project=r.project)
        if r.project:
            where |= Q(emdash_project="")
        bindings = RunnerBinding.objects.filter(runner=runner, session_key=r.session_key).filter(where)
        if r.ok:
            updated += bindings.exclude(close_error="").update(close_error="", close_error_at=None)
        else:
            reason = (r.error or "").strip() or "the runner could not close it"
            updated += bindings.update(close_error=reason[:300], close_error_at=now)
    return {"updated": updated}


@router.get("/runners/{runner_id}/menu-answers", response=MenuAnswerSyncOut)
def list_menu_answers(request: HttpRequest, runner_id: uuid.UUID):
    """Answers a human has given that this runner has not pressed yet.

    The WS control frame is the fast path; this is the one that makes the answer
    SURVIVE. A frame published while the runner's control channel is down goes to
    a Channels group with no consumer and is discarded — and because the runner
    still heartbeats over REST it reads ONLINE the whole time, so the API answers
    `ok:true` and nothing records the loss. Measured on labs 2026-08-01: an answer
    sent at 10:50 never reached the runner, between reconnects at 10:16 and 10:58.

    Drained on the poll tick the runner already runs, same as `/backfills`.
    """
    runner = runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner)
        .exclude(pending_answer__isnull=True)
        .exclude(session_key="")
    )
    return {"answers": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.emdash_project,
         "answer_id": (b.pending_answer or {}).get("id") or "",
         "option": (b.pending_answer or {}).get("option"),
         "selections": (b.pending_answer or {}).get("selections"),
         "texts": (b.pending_answer or {}).get("texts")}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/menu-answer-result", response=MenuAnswerResultOut)
def post_menu_answer_result(request: HttpRequest, runner_id: uuid.UUID,
                            payload: MenuAnswerResultIn):
    """The runner reports what became of an answer, which retires it.

    Matched on `answer_id`: applying an answer twice means a SECOND keystroke into
    a session that has already moved on, so a result for an answer that has since
    been replaced must NOT clear the newer one.
    """
    runner = runner_or_404(request, runner_id)
    binding = RunnerBinding.objects.filter(
        session_id=payload.session_id, runner=runner).first()
    if binding is None:
        return {"ok": False}
    current = (binding.pending_answer or {}).get("id")
    if not current or current != payload.answer_id:
        return {"ok": False}
    binding.pending_answer = None
    binding.save(update_fields=["pending_answer"])
    return {"ok": True}


@router.post("/runners/{runner_id}/session-menu", response=SessionMenuOut)
def post_session_menu(request: HttpRequest, runner_id: uuid.UUID, payload: SessionMenuIn):
    """A dialog the agent of one session on this runner is waiting on, or `menu:
    null` once it is not. A cloud runner's way to say what a laptop says inside
    its session report; answers come back through `/menu-answers`. `ok: false`
    when this runner does not hold the session."""
    runner = runner_or_404(request, runner_id)
    return {"ok": session_sync.report_session_menu(runner, payload.session_id, payload.menu)}


@router.post("/runners/{runner_id}/admission-notices", response=AdmissionNoticeClaimOut)
def claim_admission_notices(request: HttpRequest, runner_id: uuid.UUID,
                            payload: AdmissionNoticeClaimIn):
    """Automatic replies to rejected senders that canopy-web could not send,
    leased to this runner to send from the mailboxes it reads."""
    # The other half of an admission rule's sender notice (canopy-web#1416):
    # canopy-web cannot refresh a desktop-client token, the runner's gog can.
    # Polled from the runner's mailbox tick; `apps/admission/runner_notices.py`.
    from apps.admission import runner_notices

    runner = runner_or_404(request, runner_id)
    notices = runner_notices.claim(runner, payload.mailboxes)
    return {"notices": [runner_notices.notice_out(n) for n in notices]}


@router.post("/runners/{runner_id}/admission-notices/{notice_id}", response=MenuAnswerResultOut)
def report_admission_notice(request: HttpRequest, runner_id: uuid.UUID, notice_id: int,
                            payload: AdmissionNoticeReportIn):
    """What this runner did with an automatic reply it was leased."""
    from apps.admission import runner_notices

    runner = runner_or_404(request, runner_id)
    return {"ok": runner_notices.report(runner, notice_id, payload.outcome, payload.detail)}


@router.post("/runners/{runner_id}/session-backfill", response=BackfillWriteOut)
def post_session_backfill(request: HttpRequest, runner_id: uuid.UUID, payload: SessionBackfillIn):
    """The runner ships a session's full transcript; the server writes Message rows
    once and clears the request. Runner-owned-binding gated."""
    from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest

    runner = runner_or_404(request, runner_id)
    binding = RunnerBinding.objects.filter(session_id=payload.session_id, runner=runner).first()
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    session = Session.objects.get(pk=payload.session_id)
    session = chat_transcript_ingest.fork_if_name_reused(session, payload.transcript_id) or session
    # Same guard as the live path: history from a different transcript lands in
    # its own epoch rather than merging into what is held. Idempotent across chunks — the
    # first one records the id, so the rest of the ship matches and writes through.
    chat_transcript_ingest.ensure_transcript_identity(session, payload.transcript_id)
    written = chat_transcript_ingest.write_backfill(session, [m.dict() for m in payload.messages])
    # Only the LAST chunk clears the ask. A transcript is shipped in byte-budgeted
    # chunks (see SessionBackfillIn.final), and clearing on the first one would
    # retire the request while most of the history was still in flight — a ship
    # that then died would leave a permanently partial session with nothing left
    # to re-trigger it.
    if payload.final and binding.backfill_requested:
        binding.backfill_requested = False
        binding.save(update_fields=["backfill_requested", "updated_at"])
    return {"written": written}
