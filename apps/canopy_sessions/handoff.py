"""A transfer carries the WORK, not just the conversation.

`placement.transfer_session` moves the binding; this module moves what the source
box had in its worktree. The flow, each step a durable row the runners drain on
their poll tick (`SessionHandoff`; a WS frame would vanish on a dead channel):

1. **Ask.** `begin` (inside the transfer) records the source's session identity
   and, when the source is reachable, asks it to pack its worktree. A box that
   is out of Claude TOKENS still has a working runner — the pack is plain git,
   and the drain runs above the pause gate — so a rate-limited account can hand
   its work over. The transfer turn is NOT enqueued yet.
2. **Settle.** When the source reports (packed, or could not), or `CAPTURE_WAIT`
   passes, `settle` enqueues the transfer turn pinned to the target. Its prompt
   says plainly what came with it — or NO SNAPSHOT, and why.
3. **Restore.** The target runner unpacks the snapshot into its new worktree
   before the agent acts (the agent's first command waits for the runner's
   report), checks `git status` against the source's, and reports.
4. **Close.** Only after a verified restore does the source close its task, so a
   failed restore never costs the only copy. Until then the source's session
   report must not re-file the open task as a NEW session (`suppressed_tasks`).
   A close that fails, or a task kept open on purpose, raises an alert
   (`alerts_handoff.py`) instead of sitting in a column nobody reads.

A source that was asleep transfers with NO SNAPSHOT; if it comes back it packs
then ("late"), the snapshot is held, and its task is closed — the work is safe on
canopy-web and `restore_late` can put it into the session on demand.

A sensitive conversation's work is never packed: canopy-web would hold it in the
clear, which is the one thing a sensitive conversation promises it does not.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.harness import initiator as who
from apps.harness import sensitivity
from apps.harness import services as harness_services
from apps.harness.models import Runner, Turn

from . import services, transcript_ingest
from .models import HandoffArchive, RunnerBinding, Session, SessionHandoff, TransferRequest

logger = logging.getLogger(__name__)

#: How long a transfer waits for the source to pack before going without.
CAPTURE_WAIT = dt.timedelta(minutes=3)
#: An archive is dropped this long after it was made.
ARCHIVE_KEEP = dt.timedelta(days=14)
#: How long a transferred-away task stays out of its old box's session list.
SUPPRESS_FOR = dt.timedelta(days=14)
#: The most canopy-web holds for one snapshot (the runner caps itself the same).
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_PARTS = 32

#: The agent's first command on the target: wait for the runner's restore report.
WAIT_COMMAND = (
    'm="$(git rev-parse --absolute-git-dir)/canopy-handoff.json"; '
    'for i in $(seq 180); do [ -s "$m" ] && break; sleep 1; done; '
    'cat "$m" 2>/dev/null || echo "NO RESTORE REPORT after 3 minutes"'
)


# ---- 1. ask ---------------------------------------------------------------

def why_not_capture(session: Session, binding: RunnerBinding, source) -> str:
    """Why the source cannot be asked to pack, or "" when it can. Each reason is
    what the target agent is told, so it reads as a sentence."""
    if source is None:
        return "the session was not on any runner"
    if sensitivity.is_sensitive(session):
        return ("this is a sensitive conversation, and canopy-web never holds a sensitive "
                "conversation's work")
    if not binding.session_key:
        return f"{source.name} had no open session to pack"
    if not source.is_reachable:
        return f"{source.name} is {source.live_status} — not answering, so nothing was packed"
    return ""


def _initiator_fields(initiator) -> dict | None:
    if initiator is None:
        return None
    return {"kind": initiator.kind, "via": initiator.via, "assurance": initiator.assurance,
            "user_id": getattr(initiator.user, "pk", None), "agent_slug": initiator.agent_slug}


def _initiator_from(fields: dict | None, user):
    if not fields:
        return who.for_user(user, via="transfer", assurance="") if user else who.unknown(via="transfer")
    person = (get_user_model().objects.filter(pk=fields.get("user_id")).first()
              if fields.get("user_id") else None)
    return who.Initiator(fields.get("kind") or who.UNKNOWN, fields.get("via") or "transfer",
                         fields.get("assurance") or "", user=person,
                         agent_slug=fields.get("agent_slug") or "")


def begin(*, session: Session, binding: RunnerBinding, source, target, brief: str, user,
          initiator) -> SessionHandoff:
    """Record the handoff for a transfer that is happening now (the caller holds
    the binding lock and has NOT yet cleared the source's identity off it).

    Asks the source to pack when it can; otherwise settles at once (NO SNAPSHOT),
    leaving a close pending so a source that comes back later packs and closes."""
    reason = why_not_capture(session, binding, source)
    capturable = not reason
    holds_task = source is not None and bool(binding.session_key)
    if not holds_task:
        close_status, close_error = "", ""
    elif sensitivity.is_sensitive(session):
        close_status = SessionHandoff.KEPT
        close_error = ("left open: a sensitive conversation's work is never packed, so this "
                       "task holds the only copy — close it by hand once nothing in it is needed")
    else:
        close_status, close_error = SessionHandoff.PENDING, ""
    h = SessionHandoff.objects.create(
        session=session, source_runner=source, target_runner=target,
        source_session_key=binding.session_key, source_project=(
            binding.emdash_project or session.emdash_project or ""),
        source_transcript_id=binding.transcript_id, requested_by=user if getattr(
            user, "pk", None) else None,
        brief=brief, initiator=_initiator_fields(initiator),
        # The target resumes the conversation itself only if it runs Claude itself
        # (a cloud box); emdash owns its Claude process and cannot be told to.
        want_transcript=capturable and target.kind == Runner.CLOUD,
        capture_status=SessionHandoff.PENDING if capturable else (
            SessionHandoff.UNREACHABLE if holds_task and not source.is_reachable
            else SessionHandoff.NOT_ASKED),
        capture_note="" if capturable else reason,
        deadline=timezone.now() + CAPTURE_WAIT if capturable else None,
        # Something to close only when the source holds a task by this name. An
        # unreachable source that comes back packs late, THEN closes (`due_for`).
        close_status=close_status, close_error=close_error,
    )
    return h


def suppressed_tasks(runner) -> set[tuple[str, str]]:
    """(task, project) pairs `runner` still holds open after transferring them away.
    Its session report must not file these as new sessions — a ghost of the one
    that moved. Only while the `transfer_left_open` alert can still speak for one
    (`SUPPRESS_FOR`): a task kept open past that, holding work, shows up again as
    the session it is rather than staying invisible."""
    rows = (SessionHandoff.objects.filter(source_runner=runner,
                                          created_at__gte=timezone.now() - SUPPRESS_FOR)
            .exclude(source_session_key="")
            .exclude(close_status__in=["", SessionHandoff.CLOSED])
            .values_list("source_session_key", "source_project"))
    return {(k, p) for k, p in rows}


# ---- 2. settle ------------------------------------------------------------

def _names(paths, limit: int = 12) -> str:
    paths = list(paths or [])
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def describe_work(manifest: dict) -> str:
    """What the snapshot holds, in sentences — the target agent reads this."""
    branch = manifest.get("branch") or "a detached HEAD"
    head = str(manifest.get("head") or "")[:12]
    lines = [f"- Branch `{branch}` at `{head}`"
             + (f", with {manifest['local_commits']} local commit(s) that were never pushed"
                if manifest.get("local_commits") else
                (f" (tracking `{manifest['upstream']}`)" if manifest.get("upstream") else ""))
             + "."]
    for key, label in (("staged_files", "Staged"), ("unstaged_files", "Unstaged changes to"),
                       ("untracked_files", "Untracked files")):
        if manifest.get(key):
            lines.append(f"- {label}: {_names(manifest[key])}.")
    if not (manifest.get("staged_files") or manifest.get("unstaged_files")
            or manifest.get("untracked_files") or manifest.get("local_commits")):
        lines.append("- No uncommitted or unpushed work: only the branch needed restoring.")
    for s in manifest.get("skipped") or []:
        lines.append(f"- Left behind on purpose: `{s.get('path')}` ({s.get('why')}).")
    lines.append("- Ignored files (`.env`, build output, `node_modules`) never travel; "
                 "recreate them if the work needs them.")
    return "\n".join(lines)


def build_prompt(h: SessionHandoff, *, source_name: str, target_name: str, resumed: bool) -> str:
    from .placement import TRANSFER_PREAMBLE  # noqa: PLC0415 — cycle: placement imports this module

    if resumed:
        head = (f"**This conversation has moved from {source_name} to {target_name}.**\n\n"
                "The conversation above was carried over and you have it in context. The "
                "MACHINE changed: this is a new checkout, so paths in the conversation under "
                f"`{(h.manifest or {}).get('cwd') or 'the old checkout'}` now live under your "
                "current directory. Verify the state below yourself, report where things "
                "stand, and wait — do not start new work.\n")
    else:
        head = TRANSFER_PREAMBLE.format(source=source_name, target=target_name)
    if h.capture_status == SessionHandoff.CAPTURED and h.manifest:
        work = (
            f"\n**The work came with you.** The runner restores {source_name}'s snapshot "
            "into this worktree before you act. Your FIRST command, before anything else, "
            f"reads its report (it waits for it if the restore is still running):\n\n"
            f"```\n{WAIT_COMMAND}\n```\n\n"
            f"What {source_name} had:\n{describe_work(h.manifest)}\n\n"
            '`"ok": true` in the report means `git status` here is exactly what it was on '
            f"{source_name}. Anything else — an error, a mismatch, no report — STOP and say "
            "what it says. Do not rebuild the work from the conversation: the conversation "
            "is not a record of the files.\n")
    else:
        why = h.capture_note or f"{source_name} did not hand over its work"
        work = (
            f"\n**NO SNAPSHOT.** {why}. None of {source_name}'s uncommitted or unpushed "
            "work is in this checkout. The conversation is context only — it is NOT a "
            "record of the files, and rebuilding the work from it is a guess. Check what was "
            "pushed (`git fetch` and look for the branch), report what you find, and wait.\n")
    prompt = head + work
    if h.brief.strip():
        prompt = f"{prompt}\n{h.brief.strip()}\n"
    return prompt


def settle(h: SessionHandoff) -> SessionHandoff:
    """Enqueue the transfer turn, exactly once, now that the capture is decided.
    No-op when already settled, or when the session has since moved elsewhere."""
    with transaction.atomic():
        h = SessionHandoff.objects.select_for_update().get(pk=h.pk)
        if h.settled_at is not None:
            return h
        session = Session.objects.get(pk=h.session_id)
        binding = RunnerBinding.objects.select_for_update().filter(session=session).first()
        target = h.target_runner
        if binding is None or target is None or binding.runner_id != target.id:
            h.settled_at = timezone.now()
            h.capture_note = (h.capture_note or "superseded: the session moved again before "
                              "this transfer settled")[:500]
            h.save(update_fields=["settled_at", "capture_note"])
            return h
        if h.capture_status == SessionHandoff.PENDING:
            h.capture_status = SessionHandoff.TIMED_OUT
            h.capture_note = (f"{h.source_runner.name if h.source_runner else 'the source'} "
                              f"did not hand over its work within "
                              f"{int(CAPTURE_WAIT.total_seconds() // 60)} minutes")
        captured = h.capture_status == SessionHandoff.CAPTURED
        resume = bool(captured and h.want_transcript and (h.manifest or {}).get("transcript")
                      and target.kind == Runner.CLOUD and h.source_transcript_id)
        if resume:
            # The target resumes the SAME Claude session (its transcript, under its
            # own id), so its ordinals continue the held rows: keep the epoch and
            # the transcript id. If the box cannot resume after all, its fresh
            # session's new id opens an epoch on first ship
            # (`ensure_transcript_identity`), so nothing is lost either way.
            binding.transcript_id = h.source_transcript_id
        else:
            # A fresh Claude session: ordinals restart, so lift them above the history.
            transcript_ingest._open_epoch(binding, session)
            binding.transcript_id = ""
        binding.save(update_fields=["transcript_id", "index_offset", "updated_at"])

        source_name = h.source_runner.name if h.source_runner else "an unknown runner"
        prompt = build_prompt(h, source_name=source_name, target_name=target.name, resumed=resume)
        origin_ref = {"thread_key": binding.thread_key or str(session.id),
                      "chat_session_id": str(session.id), "transfer_from": source_name}
        if captured:
            origin_ref["handoff"] = {"id": str(h.id), "resume": resume}
            if resume:
                origin_ref["handoff"]["cold_prompt"] = build_prompt(
                    h, source_name=source_name, target_name=target.name, resumed=False)
        turn, _ = harness_services.enqueue_turn(
            session=session, origin=_origin(session),
            # The handoff's own id: transferring back and forth (the normal shape
            # of a failover) must never dedupe onto an earlier move's turn.
            idempotency_key=f"transfer:{session.id.hex}:{h.id.hex}",
            prompt=prompt, origin_ref=origin_ref, enqueued_by=h.requested_by,
            pinned_runner=target, initiator=_initiator_from(h.initiator, h.requested_by),
            parent={"session": session}, provenance_extra={"transfer_from": source_name},
        )
        h.turn, h.settled_at, h.resume_planned = turn, timezone.now(), resume
        if captured:
            h.restore_status = SessionHandoff.PENDING
        h.save(update_fields=["turn", "settled_at", "resume_planned", "restore_status",
                              "capture_status", "capture_note"])
        # A request approved before its turn existed records the turn now.
        TransferRequest.objects.filter(session=session, status=TransferRequest.APPROVED,
                                       turn_id__isnull=True).update(turn_id=turn.id)
        services._merge_metadata(session, {"requested_runner_id": str(target.id)})
    return h


def settle_overdue(now=None) -> int:
    """The off-request clock's job: settle every transfer whose source did not
    answer in time, and drop archives past `ARCHIVE_KEEP`."""
    now = now or timezone.now()
    n = 0
    for h in SessionHandoff.objects.filter(settled_at__isnull=True, deadline__lte=now):
        try:
            settle(h)
            n += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.exception("could not settle handoff %s", h.pk)
    HandoffArchive.objects.filter(created_at__lt=now - ARCHIVE_KEEP).delete()
    return n


# ---- the source's side ----------------------------------------------------

def due_for(runner) -> list[dict]:
    """What `runner` owes as a SOURCE: snapshots to pack (on time, or late — a box
    that was asleep at the transfer packs when it is back), and tasks to close."""
    #: Still worth packing: never answered, or answered too late to ride the transfer.
    unpacked = (SessionHandoff.PENDING, SessionHandoff.UNREACHABLE, SessionHandoff.TIMED_OUT)
    out = []
    rows = (SessionHandoff.objects.filter(source_runner=runner, close_status=SessionHandoff.PENDING)
            .exclude(source_session_key="").order_by("created_at")[:20])
    for h in rows:
        capture = h.capture_status in unpacked
        if capture or close_due(h):
            out.append({
                "id": str(h.id), "session_id": str(h.session_id),
                "session_key": h.source_session_key, "project": h.source_project,
                "transcript": h.want_transcript, "capture": capture, "close": not capture,
                "close_error": h.close_error,
            })
    return out


def close_due(h: SessionHandoff) -> bool:
    """Whether the source may close its task now: the work is somewhere else."""
    if h.close_status != SessionHandoff.PENDING:
        return False
    if h.close_forced or h.restore_status == SessionHandoff.RESTORED:
        return True
    # A late snapshot (the transfer already went without it): held here, so safe.
    return h.capture_status == SessionHandoff.CAPTURED and h.restore_status == ""


def receive_part(h: SessionHandoff, *, part: int, parts: int, data: bytes) -> int:
    """Append one chunk of the archive. Parts arrive in order; part 0 restarts."""
    if not 0 < parts <= MAX_PARTS or not 0 <= part < parts:
        raise ValueError("bad part numbering")
    with transaction.atomic():
        h = SessionHandoff.objects.select_for_update().get(pk=h.pk)
        if h.capture_status == SessionHandoff.CAPTURED:
            raise ValueError("this snapshot was already received")
        archive, _ = HandoffArchive.objects.select_for_update().get_or_create(handoff=h)
        if part == 0:
            archive.data, h.parts_received = b"", 0
        elif part != h.parts_received:
            raise ValueError(f"expected part {h.parts_received}, got {part}")
        body = bytes(archive.data) + data
        if len(body) > MAX_ARCHIVE_BYTES:
            raise ValueError(f"snapshot over the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB cap")
        archive.data = body
        archive.save(update_fields=["data"])
        h.parts_received = part + 1
        h.save(update_fields=["parts_received"])
        return h.parts_received


def record_capture(h: SessionHandoff, *, ok: bool, manifest: dict | None, error: str) -> SessionHandoff:
    """The source's answer. On success the archive must be complete and match the
    manifest's hash; a failure keeps the source's task OPEN (it holds the only
    copy) and says why."""
    with transaction.atomic():
        h = SessionHandoff.objects.select_for_update().get(pk=h.pk)
        if h.capture_status == SessionHandoff.CAPTURED:
            return h
        now = timezone.now()
        if ok:
            archive = HandoffArchive.objects.filter(handoff=h).first()
            data = bytes(archive.data) if archive else b""
            want = (manifest or {}).get("sha256") or ""
            if not data or hashlib.sha256(data).hexdigest() != want:
                ok, error = False, "the uploaded snapshot did not match its checksum"
        late = h.settled_at is not None
        if ok:
            h.capture_status, h.manifest, h.captured_at = SessionHandoff.CAPTURED, manifest, now
            h.capture_note = ("packed after the transfer had already gone without it"
                              if late else "")
        else:
            HandoffArchive.objects.filter(handoff=h).delete()
            if not late:
                h.capture_status = SessionHandoff.FAILED
                h.capture_note = f"{h.source_runner.name if h.source_runner else 'the source'} " \
                                 f"could not pack its work: {error}"[:500]
            if h.close_status == SessionHandoff.PENDING:
                h.close_status = SessionHandoff.KEPT
                h.close_error = (f"left open: its work could not be packed ({error}), so this "
                                 "task holds the only copy")[:500]
        h.save()
    if not h.settled_at:
        h = settle(h)
    return h


def record_close(h: SessionHandoff, *, ok: bool, error: str) -> SessionHandoff:
    if ok:
        h.close_status, h.close_error, h.closed_at = SessionHandoff.CLOSED, "", timezone.now()
    else:
        # Stays pending: the next tick retries. The reason is what the alert says.
        h.close_error = (error or "the runner could not close it")[:500]
    h.save(update_fields=["close_status", "close_error", "closed_at"])
    return h


# ---- the target's side ----------------------------------------------------

def record_restore(h: SessionHandoff, report: dict) -> SessionHandoff:
    """The target's report. A verified restore lets the source close its task; any
    other outcome keeps it open, because it still holds the work."""
    ok = bool(report.get("ok"))
    if ok:
        status = SessionHandoff.RESTORED
    elif report.get("restored"):
        status = SessionHandoff.MISMATCH
    else:
        status = SessionHandoff.FAILED
    h.restore_status, h.restore_report, h.restored_at = status, report, timezone.now()
    fields = ["restore_status", "restore_report", "restored_at"]
    if not ok and h.close_status == SessionHandoff.PENDING and not h.close_forced:
        h.close_status = SessionHandoff.KEPT
        h.close_error = (f"left open: {h.target_runner.name if h.target_runner else 'the target'} "
                         f"could not restore its work exactly "
                         f"({report.get('error') or report.get('mismatch')})")[:500]
        fields += ["close_status", "close_error"]
    h.save(update_fields=fields)
    return h


def restore_late(h: SessionHandoff, *, user, initiator=None):
    """Put a snapshot that arrived after its transfer settled into the session now:
    a turn on the session's current runner carrying it. Raises ValueError when
    there is nothing to restore, or the session is no longer where it can run."""
    from . import placement  # noqa: PLC0415 — cycle: placement imports this module

    if h.capture_status != SessionHandoff.CAPTURED or not HandoffArchive.objects.filter(
            handoff=h).exists():
        raise ValueError("this handoff holds no snapshot to restore")
    session = h.session
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None:
        raise ValueError("the session is not on a runner")
    if Turn.objects.filter(chat_session=session, status__in=list(Turn.NON_TERMINAL)).exists():
        raise RuntimeError("a turn is still queued or running in this session — let it finish")
    source_name = h.source_runner.name if h.source_runner else "the old runner"
    prompt = (f"**{source_name}'s work has arrived.** It was packed after this session "
              f"moved, so the runner now restores it into this worktree — if this worktree "
              f"has no uncommitted changes. Read its report first:\n\n```\n{WAIT_COMMAND}\n```\n\n"
              f"What {source_name} had:\n{describe_work(h.manifest or {})}\n\n"
              "If the report is not `\"ok\": true`, say what it says and wait.\n")
    h.restore_status, h.restore_report = SessionHandoff.PENDING, None
    h.save(update_fields=["restore_status", "restore_report"])
    turn, _ = harness_services.enqueue_turn(
        session=session, origin=_origin(session),
        idempotency_key=f"handoff-restore:{h.id.hex}:{int(timezone.now().timestamp())}",
        prompt=prompt,
        origin_ref={"thread_key": binding.thread_key or str(session.id),
                    "chat_session_id": str(session.id),
                    "handoff": {"id": str(h.id), "resume": False}},
        enqueued_by=user, pinned_runner=binding.runner,
        initiator=placement._initiator(initiator, user, "transfer"),
        parent={"session": session},
    )
    return turn


def _origin(session) -> str:
    """The product the session's work belongs to (`send.default_origin`) — an ace-web
    session moved to another box is still ace-web work, not canopy chat."""
    from .send import default_origin  # noqa: PLC0415 — cycle: send imports placement, which imports this module

    return default_origin(session)


def summary(h: SessionHandoff) -> dict:
    """The handoff as every reader sees it (API, MCP, the transfer's answer)."""
    m = h.manifest or {}
    return {
        "id": str(h.id), "session_id": str(h.session_id),
        "source_runner": h.source_runner.name if h.source_runner_id else "",
        "target_runner": h.target_runner.name if h.target_runner_id else "",
        "capture_status": h.capture_status, "capture_note": h.capture_note,
        "branch": m.get("branch") or "", "head": m.get("head") or "",
        "local_commits": int(m.get("local_commits") or 0),
        "staged_files": m.get("staged_files") or [], "unstaged_files": m.get("unstaged_files") or [],
        "untracked_files": m.get("untracked_files") or [], "skipped": m.get("skipped") or [],
        "snapshot_bytes": int(m.get("bytes") or 0),
        "transcript_carried": bool(m.get("transcript")), "resume_planned": h.resume_planned,
        "turn_id": str(h.turn_id) if h.turn_id else "",
        "restore_status": h.restore_status, "restore_report": h.restore_report,
        "close_status": h.close_status, "close_error": h.close_error,
        "created_at": h.created_at, "settled_at": h.settled_at,
    }
