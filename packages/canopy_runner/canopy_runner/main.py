"""Runner main loop (CDP executor).

One iteration (run_once):
  1. preflight emdash's CDP health — unhealthy => degraded heartbeat, skip the
     claim (queued turns wait rather than being claimed-then-burned), still poll
     the inbox + fire schedules so inbound work keeps enqueuing
  2. heartbeat (with the macOS host, for session-reuse ownership)
  3. report the open emdash sessions the phone can continue (throttled)
  4. claim at most one queued turn and route it to an emdash session (reuse or
     create) via execute.execute_turn

Agent/project turns finish synchronously — the runner owns the routing lifecycle;
the work continues in the visible emdash session — so there is NO injection state to
track and NO emdash-DB write. CHAT turns are the exception: they stay EXECUTING while
the agent works and are pumped tick by tick (_pump_chat_bridges), because their reply
has to be carried back into the ledger and an agent turn lasts minutes, which is far
longer than a tick may block. The only emdash-DB access is the two READ-ONLY queries in
`emdash.py` (task_state, list_open_sessions), whose column dependencies are
verified out-of-band by `canopy_runner verify-emdash`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path

from . import cdp_control, chat_bridge, emdash, hook_install, menu, transcript
from .client import Client, ClientError
import canopy_transcript as transcript_core

from .tail import TailReader
from .config import Config

logger = logging.getLogger("canopy_runner")

# CDP-down throttle. The runner is otherwise stateless (no state file — see
# run_once), but the human-facing "emdash is down" WARNING must fire ONCE per
# outage, not per tick, so this small counter lives at module scope for the loop
# process's lifetime. The per-tick machine signal is the degraded heartbeat (a status
# field, not spam); this gates only the one loud log. Emit it after this many
# consecutive unhealthy ticks so a brief emdash restart (a tick or two) doesn't cry wolf.
CDP_DOWN_SIGNAL_TICKS = 3
_cdp_down_ticks = 0
_cdp_down_signalled = False
_last_session_report = 0.0
_last_branch_check = 0.0
_cached_branch = ""

# RC-cancel: turn ids the user asked to stop, relayed down the wake listener's
# control channel as `{"type": "cancel", "turn_id": ...}` frames (see WakeListener's
# on_control). The bridge poll loop (Task 8) checks membership here to interrupt a
# running turn; module-scoped so the wake listener's callback and the executor share
# one set for the process's lifetime without threading extra state through the loop.
CANCELLED_TURNS: set[str] = set()


def _code_branch(now_fn=time.monotonic) -> str:
    """The git branch of the runner's OWN checkout (best-effort, throttled+cached).

    Reported on the heartbeat so the supervisor can SHOUT when another process has
    left this runner on a non-main branch — i.e. the daemon is silently executing
    stale/wrong code (observed twice: a DDD run checked out a branch in the runner's
    shared checkout). Empty string if it can't be determined (not a git checkout, git
    missing); never raises — a heartbeat must not depend on this."""
    global _last_branch_check, _cached_branch
    if now_fn() - _last_branch_check < 15:
        return _cached_branch
    _last_branch_check = now_fn()
    try:
        repo = Path(__file__).resolve().parents[3]  # …/packages/canopy_runner/canopy_runner/main.py -> repo root
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        _cached_branch = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — best-effort; never break the heartbeat
        _cached_branch = ""
    return _cached_branch


def _reset_cdp_health_state() -> None:
    """Clear the CDP-down throttle (on recovery, and between tests)."""
    global _cdp_down_ticks, _cdp_down_signalled
    _cdp_down_ticks = 0
    _cdp_down_signalled = False


def _paused_agents(cfg: Config) -> set[str]:
    """Per-agent pause: agent slugs with a `PAUSED.<slug>` sentinel next to the state
    file (dropped by the menu-bar app). Distinct from the global `PAUSED` file, which
    halts everything. A paused agent's inbox is skipped and its queued turns are not
    claimed (the server excludes them), so its work simply waits until resumed."""
    d = Path(cfg.state_path).parent if cfg.state_path else Path.home() / ".canopy"
    try:
        return {p.name[len("PAUSED."):] for p in d.glob("PAUSED.*")}
    except OSError:
        return set()


def _maybe_check_inboxes(cfg: Config, client: Client, now_fn=time.time,
                         paused: set[str] | None = None) -> None:
    """Deterministic email trigger: at most every inbox_poll_seconds, poll each
    configured mailbox and enqueue email-origin turns. Best-effort — a failing inbox
    (auth expired) logs and is skipped, never crashes the loop. Paused agents are
    skipped so no new email turns are enqueued for them."""
    if not getattr(cfg, "mailboxes", None):
        return
    stamp = Path(cfg.state_path).with_name("inbox-last.txt") if cfg.state_path else Path("inbox-last.txt")
    try:
        last = float(stamp.read_text())
    except (OSError, ValueError):
        last = 0.0
    if now_fn() - last < cfg.inbox_poll_seconds:
        return
    from . import inbox as inbox_mod
    cap = getattr(cfg, "inbox_max_threads", 8)
    for agent, box in cfg.mailboxes.items():
        if paused and agent in paused:
            continue
        try:
            res = inbox_mod.check_inbox(
                client, agent, mailbox=box["account"], gog_client=box["client"],
                query=box.get("query", inbox_mod.DEFAULT_QUERY), max_threads=cap,
            )
            n_new, n_seen = len(res["new"]), len(res["seen"])
            n_skip = len(res.get("skipped", []))
            # Log EVERY poll, not just ones that enqueue — otherwise a healthy poll that
            # finds nothing new is silent and you can't tell polling is happening at all.
            # `skipped` = unread threads whose newest message is the agent's own reply
            # (already had the last word), suppressed so a re-marked-unread thread can't
            # manufacture a turn with no new inbound.
            logger.info("inbox[%s]: polled — %d unread (%d NEW -> session, %d already tracked, "
                        "%d skipped: agent's own reply)",
                        agent, n_new + n_seen + n_skip, n_new, n_seen, n_skip)
        except Exception as exc:  # noqa: BLE001 — one bad inbox never kills the loop
            logger.warning("inbox check for %s failed: %s", agent, exc)
    try:
        stamp.write_text(str(now_fn()))
    except OSError:
        pass


def _fire_due_schedules(cfg: Config, client: Client, paused: set[str] | None = None) -> None:
    """Scheduled-turn trigger: sync the schedules this runner may fire, evaluate each
    cron locally, and report any due slot so the server materializes the turn.

    Unthrottled on purpose — unlike the inbox (a subprocess per mailbox), this is one
    HTTP GET, the same cost class as the claim it rides alongside, and the poll IS the
    tick: throttling it would just add latency to every slot. Best-effort — a failing
    sync (server down, token expired) logs and is skipped, never crashes the loop.

    Only reached when NOT globally paused: main()'s pause sentinel `continue`s before
    run_once, so a paused runner never fires (which would queue turns that all execute
    the instant it resumes). Per-agent pause is honored inside check_schedules.
    """
    now = dt.datetime.now(dt.UTC)
    try:
        # Import INSIDE the guard, not above it: canopy_cron is scheduling's ONLY
        # dependency, and a missing/broken one (an un-synced laptop env, a bad
        # install) must disable scheduling alone — not crash claiming and the inbox
        # with it. The import is the most likely failure, so it has to be caught too.
        from . import schedules as schedules_mod
        schedules_mod.check_schedules(client, cfg.runner_id, now=now, paused=frozenset(paused or ()))
    except Exception as exc:  # noqa: BLE001 — scheduling never kills claiming or the inbox
        logger.warning("scheduling unavailable this tick (claiming + inbox continue): %s", exc)


def _claim_and_execute(cfg: Config, client: Client, paused: set) -> str:
    """Claim at most one eligible turn and route it to an emdash session. The shared
    core of both the loop's iteration and the single-turn primitive, so they can't
    drift. Returns reused:/created:/failed:<id> or "idle" when nothing is queued."""
    from . import execute, readiness

    try:
        turn = client.claim(cfg.runner_id, paused_agents=sorted(paused))
    except ClientError as exc:
        logger.warning("claim failed: %s", exc)
        return "idle"
    if turn is None:
        return "idle"
    try:
        return execute.execute_turn(
            cfg, client, cfg.runner_id, turn,
            cancel_check=lambda tid: tid in CANCELLED_TURNS,
        )
    except Exception as exc:  # noqa: BLE001 — one turn must never kill the loop
        logger.exception("execute_turn crashed for %s", turn.get("id"))
        note = f"runner execute crashed: {exc}"
        readiness.mark_failed(cfg, note)
        try:
            client.fail_turn(turn["id"], note)
        except ClientError:
            pass
        return f"failed:{turn.get('id')}"
    finally:
        # Evict once the turn is done, regardless of outcome — CANCELLED_TURNS is a
        # transient "stop now" signal, not a durable per-turn record; leaving an id in
        # it forever would wrongly mark any FUTURE turn that reused the same id (turn
        # ids aren't reused today, but leaking membership is a latent footgun either
        # way — and it just keeps a module-level set growing unbounded for the life
        # of the process).
        CANCELLED_TURNS.discard(turn["id"])


# Per-session incremental tail readers, keyed by emdash_task — the byte-offset change
# signal that makes the phone reflect live emdash activity (see _session_changed).
_tail_readers: dict[str, "TailReader | None"] = {}

# Per-session live-stream tailers, keyed by session_id — active only while a viewer
# is attached (stream_desired on the server). Distinct from _tail_readers (the idle
# tail read-model that fills RunnerBinding.tail); this is the live push to attached
# viewers. Each entry: {"reader": TailReader|None, "count": int (records consumed ==
# the next record's ordinal), "session_key": str, "project": str, "last_index":
# int|None (the server's catch-up marker)}. Deliberately holds NO durable resume
# state — the server's last_index is the checkpoint (spec 2026-07-24).
_stream_readers: dict[str, dict] = {}


def _session_changed(cfg: Config, sessions: list[dict]) -> bool:
    """True if any SHOWN session's transcript grew, or a new session appeared, since
    the last check. This is the LIVE signal — cheap (a byte-offset read of only the
    newly-appended bytes) and it catches assistant streaming too (transcript growth),
    not just user interaction (which is all last_interacted_at would catch)."""
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    active: set[str] = set()
    changed = False
    for s in sessions[: cfg.session_tail_count]:  # only the sessions the phone shows
        task = s.get("emdash_task")
        if not task:
            continue
        active.add(task)
        first_sight = task not in _tail_readers
        tr = _tail_readers.get(task)
        if tr is None:  # unresolved (new session, or transcript not found yet) — (re)try
            path = transcript.resolve_transcript(
                s.get("project") or "", task, home=home, claude_home=claude_home
            )
            tr = TailReader(str(path)) if path else None
            if tr is not None:
                tr.seek_end()  # stream only NEW activity from here, never the history
            _tail_readers[task] = tr
        if first_sight:
            changed = True  # a session newly appeared
        elif tr is not None and tr.read_new():
            changed = True  # its transcript grew
    for task in list(_tail_readers):  # drop readers for sessions that are gone
        if task not in active:
            _tail_readers.pop(task, None)
    return changed


def _maybe_report_sessions(cfg: Config, client: Client, now_fn=time.monotonic) -> None:
    """Report the open emdash sessions the phone can continue. CHANGE-DRIVEN: reports
    the instant a shown session's transcript grows (so the phone reflects live emdash
    activity within a poll tick), plus a heartbeat every session_report_seconds so a
    freshly-connected phone gets state. The cheap change-check runs every tick; the
    expensive recent-tail read + POST only on a real change or the heartbeat. A sqlite
    read of emdash's DB (runs even while CDP is down); best-effort — never stops a tick."""
    global _last_session_report
    try:
        sessions = emdash.list_open_sessions(cfg.emdash_db, cfg.session_report_limit)
    except emdash.EmdashReadError:
        # WARNING, not debug: this is the silent-degradation class verify-emdash
        # exists for. Skip the report entirely — an empty one would clear every
        # RunnerBinding server-side, which is the opposite of what we observed.
        logger.warning(
            "emdash session read FAILED — skipping this report so the server keeps the "
            "sessions it already knows. Run `canopy-runner verify-emdash` to check for "
            "schema drift.",
            exc_info=True,
        )
        return
    except Exception:  # noqa: BLE001
        logger.debug("session list failed (non-fatal)", exc_info=True)
        return
    changed = _session_changed(cfg, sessions)
    heartbeat = now_fn() - _last_session_report >= cfg.session_report_seconds
    if not changed and not heartbeat:
        return
    _last_session_report = now_fn()
    # Read the closing signal only on a tick we're actually going to report on.
    # Fail-soft in the opposite direction to the open-session read: losing the
    # archived list must not cost us the report, so omit the field and carry on.
    try:
        archived = emdash.list_recently_archived_tasks(
            cfg.emdash_db, cfg.session_report_limit
        )
    except emdash.EmdashReadError:
        logger.debug("archived-task read failed (non-fatal, omitting)", exc_info=True)
        archived = []
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        client.report_sessions(cfg.runner_id, sessions, archived)
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)


def _finish_chat_bridge(cfg: Config, client: Client, bridge, *, status: str, note: str) -> None:
    """Retire one in-flight bridge: drop it from the registry FIRST (so a failing
    finish can't leave it pumping forever), interrupt the live session on a cancel,
    then tell the server. Best-effort — a client hiccup must not wedge the loop."""
    from . import cdp_control

    chat_bridge.IN_FLIGHT.pop(bridge.turn_id, None)
    CANCELLED_TURNS.discard(bridge.turn_id)
    if status == "cancelled":
        try:
            cdp_control.interrupt(bridge.task, port=cfg.cdp_port)
        except Exception as exc:  # noqa: BLE001 — cancel must still finish the turn
            logger.warning("chat turn=%s: interrupt failed: %s", bridge.turn_id, exc)
    try:
        client.finish(bridge.turn_id, note=note, status=status)
    except Exception:  # noqa: BLE001
        logger.warning("chat turn=%s: finish failed", bridge.turn_id, exc_info=True)
    logger.info("chat turn=%s %s (task=%s): %s", bridge.turn_id, status, bridge.task, note)


def _pump_chat_bridges(cfg: Config, client: Client) -> None:
    """Advance every in-flight chat turn by one tick: ship whatever the agent has
    written since the last tick, and finish the turn once the transcript says it
    handed the floor back (see chat_bridge.hands_back_to_human).

    This is why a chat turn no longer blocks the loop. It used to run inline inside
    execute_chat_turn, which was only survivable because it gave up after 3 seconds
    of transcript silence — and giving up after 3s is exactly what truncated every
    answer that involved a tool call. Pumping it here buys the correct completion
    rule without holding the tick: the heartbeat, claims and session reports all
    keep running while an agent works.
    """
    for turn_id, bridge in list(chat_bridge.IN_FLIGHT.items()):
        if turn_id in CANCELLED_TURNS:
            _finish_chat_bridge(cfg, client, bridge, status="cancelled", note="cancelled by user")
            continue
        try:
            new_records = bridge.reader.read_new()
            raw_lines = list(getattr(bridge.reader, "last_raw", ()) or ())
        except Exception:  # noqa: BLE001 — an unreadable transcript is a quiet tick
            logger.debug("chat turn=%s: transcript read failed", turn_id, exc_info=True)
            new_records, raw_lines = [], []
        bridge.step(new_records, raw_lines)
        _flush_turn_transcript(client, bridge)
        if bridge.pending:
            events = [{"kind": "assistant", "payload": {"text": t}} for t in bridge.pending]
            try:
                client.post_events(turn_id, events)
                bridge.pending.clear()   # only drop text the server has actually taken
            except Exception:  # noqa: BLE001 — keep it queued and retry next tick
                logger.debug("chat turn=%s: event post failed, retrying", turn_id, exc_info=True)
        if bridge.finished:
            # One last attempt before the turn closes: whatever the agent wrote in
            # its final tick is exactly the part a cost aggregator needs (the
            # `result` line carries the turn's totals).
            _flush_turn_transcript(client, bridge, final=True)
            _finish_chat_bridge(cfg, client, bridge, status="done", note=bridge.note)


def _flush_turn_transcript(client: Client, bridge, *, final: bool = False) -> None:
    """Ship the bridge's accumulated raw JSONL to the turn's retained transcript.

    Best-effort and non-blocking for the turn: the reply is the product, this is
    a derived artifact. A failed batch stays queued and is retried next tick; on
    the FINAL flush an unshippable batch is dropped rather than holding the turn
    open, and says so in the log.

    Batches carry `<turn>:<n>` as their batch_id so a lost-ack retry dedupes
    server-side instead of double-appending (apps/harness/services.append_transcript).
    """
    if bridge.transcript_truncated or not bridge.raw_pending:
        return
    for batch in chat_bridge.chunk_raw_lines(bridge.raw_pending):
        batch_id = f"{bridge.turn_id}:{bridge.raw_batches_sent}"
        try:
            still_open = client.post_transcript(bridge.turn_id, batch, batch_id)
        except Exception:  # noqa: BLE001
            if final:
                _note_failure(f"transcript:{bridge.turn_id}",
                              f"final transcript flush ({len(batch)} lines, dropped)")
                bridge.raw_pending.clear()
            else:
                _note_failure(f"transcript:{bridge.turn_id}", "transcript flush")
            return
        bridge.raw_batches_sent += 1
        del bridge.raw_pending[:len(batch)]
        if not still_open:
            # Per-turn ceiling reached; every further batch is a server-side
            # no-op, so stop paying for them.
            bridge.transcript_truncated = True
            bridge.raw_pending.clear()
            logger.info("chat turn=%s: transcript ceiling reached; no further flushes",
                        bridge.turn_id)
            return
    _note_success(f"transcript:{bridge.turn_id}")


def _drain_chat_bridges(cfg: Config, client: Client, *, poll: float = 1.0,
                        max_seconds: float = 3600.0) -> None:
    """Pump to completion, for the ONE-SHOT modes (--once / --drain-one) that exit
    when they return. The daemon never calls this — it pumps on its own ticks. The
    process would otherwise leave the turn EXECUTING until the server's lease sweep
    reclaimed it, so a `--drain-one` chat turn would never deliver its reply."""
    deadline = time.monotonic() + max_seconds
    last_hb = time.monotonic()
    while chat_bridge.IN_FLIGHT and time.monotonic() < deadline:
        _pump_chat_bridges(cfg, client)
        if not chat_bridge.IN_FLIGHT:
            return
        if time.monotonic() - last_hb >= 60:
            # Renew the turn lease: nothing else heartbeats in a one-shot run, and a
            # long reply would otherwise outlive it and be swept mid-answer.
            try:
                client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT))
            except Exception:  # noqa: BLE001
                logger.debug("drain heartbeat failed (non-fatal)", exc_info=True)
            last_hb = time.monotonic()
        time.sleep(poll)


# Persistent per-key failure counters for the best-effort transcript paths
# (stream posts, backfill posts). These retry every tick forever by design, so
# logging each failure would spam and logging none is what actually happened:
# the NUL-byte bug (2026-07-26) was a hard 500 on every backfill attempt for one
# session, invisible in the runner log because the handler logged at DEBUG. A
# failure that REPEATS is the interesting kind — it means stuck, not flaky.
_failures: dict[str, int] = {}

# Warn on the first failure, then every Nth, so a permanently-stuck session stays
# visible in the log without drowning it (~5 min apart at the default tick).
_REWARN_EVERY = 60


def _note_failure(key: str, what: str) -> None:
    """Count a best-effort failure and log it at a level someone will see."""
    n = _failures.get(key, 0) + 1
    _failures[key] = n
    if n == 1 or n % _REWARN_EVERY == 0:
        logger.warning("%s failed (attempt %d, still retrying): %s", what, n, key,
                       exc_info=True)
    else:
        logger.debug("%s failed (attempt %d): %s", what, n, key, exc_info=True)


def _note_success(key: str) -> None:
    """Clear a failure streak; log the recovery if there was one to clear."""
    n = _failures.pop(key, 0)
    if n:
        logger.info("recovered after %d failed attempts: %s", n, key)



# --- Live hook events (spec 2026-07-27) -------------------------------------
#
# Claude Code fires a hook per tool call straight to a loopback listener this
# runner owns. That is the LIVE half of a session's record: the transcript is
# complete but lags (the docs say so explicitly), so it cannot drive a view you
# are actively watching. The transcript remains the durable record, which is
# what makes it safe for this path to drop events freely.
#
# `{(project, session_key): session_id}`, refreshed from the stream sync each
# tick — the hook reports a cwd, and this is what turns that back into a canopy
# Session.
_hook_sessions: dict[tuple[str, str], str] = {}
_hook_listener = None


# None, NOT 0.0: `time.monotonic()` is near zero early in a process's life, so
# seeding this with 0.0 reads as "already reported just now" and suppresses the
# first report for a whole window — exactly when you are watching for it, because
# you have just turned the feature on. Found on the first live enablement; the
# original test hid it by starting its fake clock at 10,000.
_last_hook_report: float | None = None
# Same cadence as the idle-cycle line: often enough to answer "is it working?"
# without turning a busy agent into a log flood.
HOOK_REPORT_SECONDS = 300


def _maybe_report_hooks(now_fn=time.monotonic) -> None:
    """Log what the hook listener has seen.

    Without this the live path is unobservable from the runner side: events are
    accepted, resolved and forwarded entirely silently, so "is it working?" can
    only be answered from the server's logs — which is exactly the question you
    have when you cannot reach them. Counters are cumulative since start.
    """
    global _last_hook_report
    listener = _hook_listener
    if listener is None:
        return
    if _last_hook_report is not None and now_fn() - _last_hook_report < HOOK_REPORT_SECONDS:
        return
    if listener.received == 0:
        # Nothing has fired yet; silence is the honest report — and crucially we
        # do NOT stamp, or an idle tick right after startup burns the whole
        # window and the first real report waits 5 minutes behind it.
        return
    _last_hook_report = now_fn()
    logger.info(
        "hooks: %d received, %d forwarded, %d dropped (cwd not a session we back), "
        "forwarding=%s",
        listener.received, listener.forwarded, listener.dropped_unknown_cwd,
        listener._forward(),
    )


def _resolve_hook_session(cwd: str) -> str:
    """A hook's cwd -> canopy session id, or "" if this isn't a session we back.

    Hooks are installed at USER level, so they fire for every Claude Code
    session on the machine. Most are not ours; returning "" is the expected
    path, not a failure.
    """
    if not cwd:
        return ""
    parsed = transcript_core.parse_emdash_worktree(cwd, home=Path.home())
    if parsed is None:
        return ""
    project, task = parsed
    # The worktree dir may carry emdash's random de-dupe suffix, so try the exact
    # name first and the stripped one second — matching against sessions we
    # actually know rather than guessing which it is.
    for candidate in transcript_core.emdash_task_candidates(task):
        session_id = _hook_sessions.get((project, candidate))
        if session_id:
            return session_id
    return ""


def _hook_task_name(cwd: str) -> str:
    """The emdash task backing this cwd, or "" — the handle CDP drives by."""
    if not cwd:
        return ""
    parsed = transcript_core.parse_emdash_worktree(cwd, home=Path.home())
    if parsed is None:
        return ""
    project, task = parsed
    for candidate in transcript_core.emdash_task_candidates(task):
        if _hook_sessions.get((project, candidate)):
            return candidate
    return ""


# The two functions below take the CDP module as an argument so the whole
# chain — screen in, keystrokes out — is testable against a captured terminal
# with no emdash, no browser and no Playwright. The `cdp_control`-bound wrappers
# under them are what the runner actually calls.


def _read_hook_menu_from(cdp, task: str, *, cdp_port: int = 9222):
    """The dialog on `task`'s screen as a plain dict, or None."""
    if not task:
        return None
    found = menu.find_menu(cdp.read_terminal(task, port=cdp_port))
    if found is None:
        return None
    return {
        "question": found.question,
        "title": found.title,
        "body": found.body,
        "selected": found.selected,
        "options": [{"number": o.number, "label": o.label} for o in found.options],
    }


def _answer_menu_with(cdp, session_key: str, option, *, cdp_port: int = 9222) -> None:
    """Press a human's answer into `session_key`'s terminal.

    Re-reads the screen FIRST and refuses an option that is not on it. A menu can
    go stale between the phone rendering it and a thumb reaching it — and a
    NUMBER typed at a session no longer showing a dialog lands in its prompt,
    where the agent reads a bare "1" as an instruction. Double-taps and two
    people answering at once both land here.
    """
    # Re-read with a settle: a single read can catch the TUI mid-render, and
    # dropping a human's tap because the footer had not painted yet is a bug
    # they experience as "the button did nothing".
    current = menu.find_menu_settled(
        lambda: cdp.read_terminal(session_key, port=cdp_port))
    if current is None:
        logger.info("menu answer for %s ignored — no dialog on screen now", session_key)
        return
    number = None if option is None else int(option)
    if not current.allows(number):
        logger.warning("menu answer %r for %s is not on the dialog now showing (%d options)",
                       number, session_key, len(current.options))
        return
    cdp.send_keys(session_key, menu.answer_keys(number), port=cdp_port)
    logger.info("answered the dialog on %s with %s", session_key,
                "Esc" if number is None else f"option {number}")


def _read_hook_menu(cwd: str, *, cdp_port: int):
    """The dialog on this session's screen, or None.

    Only called when a session reports BLOCKED: a hook can say an agent wants a
    human but never what it is asking, and emdash owns the session, so the
    question and its options exist only on the terminal.
    """
    return _read_hook_menu_from(cdp_control, _hook_task_name(cwd), cdp_port=cdp_port)


def _answer_menu(session_key: str, option, *, cdp_port: int = 9222) -> None:
    _answer_menu_with(cdp_control, session_key, option, cdp_port=cdp_port)


def _start_hook_listener(cfg: Config, client: Client):
    """Install the user-level hook and start the loopback listener.

    Returns the listener, or None when disabled (`hook_port = 0`), in which case
    any previously-installed canopy hook is REMOVED — turning the feature off
    must not leave a curl pointing at a port nothing is listening on.
    """
    global _hook_listener
    settings_path = Path.home() / ".claude" / "settings.json"
    if cfg.hook_port <= 0:
        if hook_install.remove(settings_path):
            logger.info("hook listener disabled; removed canopy's hook from %s",
                        settings_path)
        return None
    from .hook_listener import HookListener

    nonce = uuid.uuid4().hex
    # NO read_menu here, deliberately. Reading a session's screen means driving
    # emdash over CDP, and `openTask` CLICKS the task in the sidebar and focuses
    # its terminal — so wiring it to a hook meant every Notification yanked
    # emdash to whatever agent had just asked for input, mid-typing.
    #
    # Reported 2026-07-28, twice: focus taken, a few characters typed into the
    # newly-focused prompt, then the message that was meant for that task never
    # arrived. The second half is the collision guard working exactly as designed
    # — `open_and_send` found unsent text, refused to clobber it, and defaulted to
    # a fresh session — so this bug MANUFACTURED the leaked-keystroke case that
    # guard exists to catch.
    #
    # A menu can still be read on demand (`cdp_control.read_terminal`), where the
    # task switch is something a human just asked for. It cannot be read on a
    # signal, because there is no way to read a NON-active task: emdash marks no
    # task as current in the DOM (checked — no aria-current, no data-state), so
    # identifying whose screen you are looking at requires activating it first.
    listener = HookListener(
        port=cfg.hook_port, nonce=nonce,
        resolve_session=_resolve_hook_session,
        forward=lambda: cfg.forward_sessions,
    )
    listener.bind_sender(
        lambda session_id, events: client.post_session_stream(
            cfg.runner_id, session_id, events)
    )
    try:
        listener.start()
    except OSError as exc:
        # Another process already holds the port (a second runner, a stale
        # process). Live events are an overlay, so this is a warning, not fatal.
        logger.warning("hook listener could not bind :%d (%s); live events off",
                       cfg.hook_port, exc)
        return None
    hook_install.install(settings_path, port=cfg.hook_port, nonce=nonce)
    logger.info("live hook events: listener on :%d, forwarding=%s",
                cfg.hook_port, cfg.forward_sessions)
    _hook_listener = listener
    return listener


def _post_stream_rows(cfg: Config, client: Client, sid: str, rows: list[dict]) -> bool:
    """Ship conversational rows as live events. seq == index (the composite
    transcript ordinal): monotonic per session forever, so the WS-derived
    `seq:<n>` message ids can never collide across detaches, restarts, or
    failovers — including between two rows of the same transcript record."""
    events = [
        {"kind": r["role"], "seq": r["index"], "index": r["index"],
         "payload": chat_bridge.row_payload(r)}
        for r in rows
    ]
    try:
        client.post_session_stream(cfg.runner_id, sid, events)
        _note_success(f"stream:{sid}")
        return True
    except Exception:  # noqa: BLE001
        _note_failure(f"stream:{sid}", "stream post")
        return False


def _sync_session_streams(cfg: Config, client: Client) -> None:
    """Tail each session a viewer is watching and ship every new conversational
    record (user + assistant) with its transcript ordinal — the server persists
    them as the session's durable Message rows and fans the assistant frames out
    live (spec 2026-07-24).

    The resume point is SERVER-side: the descriptor's `last_index` (max persisted
    turn_index). On attach we read the transcript once and ship everything after
    it; steady state stays change-driven off TailReader (only newly-appended
    bytes). There is deliberately NO local offset checkpoint — a failed post just
    drops the tailer so the next tick re-attaches from the marker, and a runner
    restart or account failover recovers identically. Best-effort — a client
    hiccup never breaks a tick."""
    try:
        streams = client.sync_streams(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("stream sync failed (non-fatal)", exc_info=True)
        return
    desired = {s["session_id"]: s for s in streams if s.get("session_id")}
    home = Path.home()
    claude_home = home / ".claude" / "projects"

    for sid in list(_stream_readers):  # drop tailers for sessions no longer watched
        if sid not in desired:
            _stream_readers.pop(sid, None)

    # The hook path resolves a cwd against these, so refresh it wholesale here
    # rather than accumulating stale entries for detached sessions.
    _hook_sessions.clear()
    for sid, s in desired.items():
        _hook_sessions[(s.get("project") or "", s.get("session_key") or "")] = sid
        st = _stream_readers.setdefault(sid, {
            "reader": None, "count": 0,
            "session_key": s.get("session_key") or "", "project": s.get("project") or "",
        })
        # Refresh every tick: the marker advances as the server persists our posts.
        st["last_index"] = s.get("last_index")

    for sid, st in _stream_readers.items():
        reader = st["reader"]
        if reader is None:
            # (Re-)attach: read the whole file once, atomically w.r.t. this reader,
            # and catch up from the server marker. No marker yet -> stream forward
            # only (history stays the backfill's job).
            path = transcript.resolve_transcript(
                st["project"], st["session_key"], home=home, claude_home=claude_home
            )
            if not path:
                continue  # transcript wasn't there yet — retry resolving next tick
            reader = TailReader(str(path))
            records = reader.read_new()
            last = st["last_index"]
            since = chat_bridge.end_index(len(records)) if last is None else int(last)
            rows = chat_bridge.conversational_messages(records, since)
            if rows and not _post_stream_rows(cfg, client, sid, rows):
                continue  # nothing consumed; re-attach next tick
            st["reader"], st["count"] = reader, len(records)
            continue
        new_records = reader.read_new()
        if not new_records:
            continue
        base = st["count"]
        # The batch's records start at `base` in the file; the offset is applied
        # to the RECORD ordinal inside compose_index, never to the composite
        # index (adding it there would shift a row into another record's slots).
        rows = chat_bridge.conversational_messages(new_records, -1, record_offset=base)
        if rows and not _post_stream_rows(cfg, client, sid, rows):
            # Don't advance past unshipped records: reset so the next tick
            # re-attaches and catches up from the server marker.
            st["reader"], st["count"] = None, 0
            continue
        st["count"] = base + len(new_records)


def _drain_backfills(cfg: Config, client: Client) -> None:
    """Ship full transcript history — with ordinals, so the server upsert-fills the
    older rows around anything the live stream already persisted. Best-effort — a
    missing transcript or a client hiccup is skipped, not fatal."""
    try:
        backfills = client.sync_backfills(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("backfill sync failed (non-fatal)", exc_info=True)
        return
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    for b in backfills:
        sid = b.get("session_id")
        path = transcript.resolve_transcript(
            b.get("project") or "", b.get("session_key") or "", home=home, claude_home=claude_home
        )
        if not (sid and path):
            continue  # transcript not resolvable -> leave it; server keeps showing the tail
        messages = chat_bridge.conversational_messages(chat_bridge.read_records(path), -1)
        try:
            client.post_session_backfill(cfg.runner_id, sid, messages)
            _note_success(f"backfill:{sid}")
        except Exception:  # noqa: BLE001
            # A backfill that keeps failing never rebuilds the session's history
            # and never stops trying — exactly the case that must not be silent.
            _note_failure(f"backfill:{sid}", f"backfill post ({len(messages)} rows)")


def run_once(cfg: Config, client: Client) -> str:
    """One loop iteration: preflight emdash's CDP health → heartbeat (with macOS host, for
    reuse ownership) → pump in-flight chat replies → claim one turn → route it to an
    emdash session (reuse or create). Agent/project turns finish synchronously (the
    runner owns the routing lifecycle; work continues in the visible session); a chat
    turn is registered with the pump and finishes on a later tick, when its transcript
    says the agent handed the floor back.

    Self-heal: the runner CONNECTS to emdash, it never launches it, so a closed/crashed
    emdash (or one launched without --remote-debugging-port) can't run work. If we claimed
    anyway, execute would hit the CDP-connect failure and fail the turn — and a failed turn
    is NOT auto-re-claimed, so one outage burned a turn per hit agent (real incident
    2026-07-17: 11 turns). So we PREFLIGHT: an unhealthy CDP skips the claim for this tick,
    leaving queued turns queued to auto-drain when emdash returns. Inbox + schedule polling
    still run (inbound work keeps ENQUEUING); only the claim is gated."""
    from . import readiness
    from .cdp_control import cdp_healthy, host_id

    global _cdp_down_ticks, _cdp_down_signalled
    healthy = cdp_healthy(port=cfg.cdp_port)
    host = host_id()
    if healthy:
        if _cdp_down_signalled:
            logger.info("emdash CDP healthy again on :%s — resuming claims after %d down tick(s)",
                        cfg.cdp_port, _cdp_down_ticks)
        _reset_cdp_health_state()
        _ready, _rnote = readiness.compute(cfg)
        # Report in-flight chat turns so the server renews their lease: a bridged
        # turn now outlives the tick that started it, and an unrenewed lease is swept.
        client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT), host=host,
                         ready=_ready, ready_note=_rnote, code_branch=_code_branch())
    else:
        _cdp_down_ticks += 1
        # Degraded heartbeat EVERY unhealthy tick — the machine-readable surface signal the
        # control plane + menu-bar app read ("alive but can't execute"). It's a status field,
        # overwritten each tick, so it is not spam.
        client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT), degraded=True,
                         note=f"emdash CDP unreachable on :{cfg.cdp_port} — not claiming",
                         host=host, ready=False,
                         ready_note=f"emdash CDP unreachable on :{cfg.cdp_port}",
                         code_branch=_code_branch())
        # ...and ONE loud WARNING after sustained downtime (not per tick), for the human log.
        if _cdp_down_ticks >= CDP_DOWN_SIGNAL_TICKS and not _cdp_down_signalled:
            logger.warning(
                "emdash CDP unreachable on 127.0.0.1:%s for %d consecutive ticks — SKIPPING "
                "the claim so queued turns wait instead of failing. Launch emdash with "
                "--remote-debugging-port=%s; the backlog auto-drains when it returns.",
                cfg.cdp_port, _cdp_down_ticks, cfg.cdp_port)
            _cdp_down_signalled = True

    # Before the reports: an in-flight reply is the freshest thing on this box, and
    # finishing a turn here frees the session for the next message. Runs even while
    # CDP is down — the transcript keeps growing whether or not we can drive emdash.
    _maybe_report_hooks()
    _pump_chat_bridges(cfg, client)
    _maybe_report_sessions(cfg, client)
    _sync_session_streams(cfg, client)
    _drain_backfills(cfg, client)
    paused = _paused_agents(cfg)
    # Inbound triggers run whether or not CDP is up, so inbound work still ENQUEUES while
    # emdash is down (it just waits, queued, until emdash is back). Only the claim is gated.
    _maybe_check_inboxes(cfg, client, paused=paused)
    # Fleet-audit review ingestion was removed when Ada moved to Items: approving
    # an Item dispatches its work server-side (in the decide transaction), so there
    # is no resolved review for the runner to poll. DDD findings reviews are applied
    # by the DDD orchestrator, never here.
    _fire_due_schedules(cfg, client, paused=paused)
    if not healthy:
        return "cdp_down"  # nothing claimed -> nothing burned; queued turns stay queued
    return _claim_and_execute(cfg, client, paused)


def drain_one(cfg: Config, client: Client) -> str:
    """Take exactly ONE queued turn, then exit — the "take a single turn" primitive.

    Unlike --once (a full loop iteration), this does NOT poll the inbox or fire
    schedules, so it can only run a turn that is ALREADY queued (dispatch one from the
    composer/API first); it never enqueues or spawns work you didn't ask for. It also
    runs while the daemon is paused — the global PAUSED sentinel gates main()'s loop, not
    this — so you can take one turn with the fleet otherwise off. Per-agent pauses ARE
    honoured (the claim skips a paused agent's turns)."""
    from . import readiness
    from .cdp_control import cdp_healthy, host_id

    # Same self-heal as the loop: claiming with emdash down would immediately fail (=burn)
    # the turn. Refuse instead — the caller re-runs once emdash is back on its debug port.
    if not cdp_healthy(port=cfg.cdp_port):
        logger.warning("emdash CDP unreachable on :%s — refusing to claim a turn (it would "
                       "immediately fail). Launch emdash with --remote-debugging-port=%s.",
                       cfg.cdp_port, cfg.cdp_port)
        client.heartbeat(cfg.runner_id, [], degraded=True,
                         note=f"emdash CDP unreachable on :{cfg.cdp_port}", host=host_id(),
                         ready=False, ready_note=f"emdash CDP unreachable on :{cfg.cdp_port}")
        return "cdp_down"
    _ready, _rnote = readiness.compute(cfg)
    client.heartbeat(cfg.runner_id, [], host=host_id(), ready=_ready, ready_note=_rnote)
    action = _claim_and_execute(cfg, client, _paused_agents(cfg))
    _drain_chat_bridges(cfg, client)  # one-shot: pump the reply out before exiting
    return action


def verify_emdash(cfg_path: Path) -> int:
    """Read-only check that emdash's DB still has the columns the CDP-path reads
    depend on. Exit 0 = intact; 1 = drifted (names each missing column); 2 = the
    DB itself couldn't be read.

    This is the ONE emdash assumption that fails SILENTLY. task_state() and
    list_open_sessions() swallow sqlite errors (a read failure must never be mistaken
    for "session gone"), so a renamed tasks/projects column doesn't crash — it quietly
    degrades the runner into spawning duplicate sessions and blanking the supervisor,
    with nothing in the log. Everything else we assume about emdash fails LOUDLY and is
    obvious within a tick (emdash not installed → won't launch; CDP down → degraded
    heartbeat + a WARNING; transcripts unreadable → visible). So this verifies the quiet
    one. Run it after an emdash update.
    """
    raw = json.loads(Path(cfg_path).read_text())
    db = raw.get("emdash_db")
    if not db:
        print(f"✗ no 'emdash_db' in {cfg_path}"); return 2
    try:
        problems = emdash.check_read_schema(db)
    except emdash.SchemaCheckError as exc:
        print(f"✗ {exc}"); return 2
    if problems:
        print("✗ emdash read schema drifted — the CDP-path reads would SILENTLY degrade:")
        for p in problems:
            print(f"    - {p}")
        print("  fix: reconcile task_state()/list_open_sessions() in canopy_runner/emdash.py")
        print("       against emdash's new schema, then update READ_SCHEMA to match.")
        return 1
    n = sum(len(c) for c in emdash.READ_SCHEMA.values())
    print(f"✓ emdash read schema intact — all {n} columns across "
          f"{', '.join(emdash.READ_SCHEMA)} present in {db}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="canopy runner (emdash adapter)")
    # Top-level --config/--once keep the bare invocation (no subcommand) working —
    # the launchd plist invokes `-m canopy_runner.main --config ...` with no
    # subcommand, and that must keep behaving like `run`.
    parser.add_argument("--config", help="path to runner.json")
    parser.add_argument("--once", action="store_true", help="single iteration (for cron/tests)")
    parser.add_argument("--drain-one", action="store_true",
                        help="claim + run exactly ONE queued turn, then exit (no inbox poll, "
                             "no schedules; runs even while paused)")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="run the main loop (default)")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--once", action="store_true", help="single iteration (for cron/tests)")
    run_parser.add_argument("--drain-one", action="store_true",
                            help="claim + run exactly ONE queued turn, then exit")

    verify_parser = subparsers.add_parser(
        "verify-emdash",
        help="read-only check that emdash's DB still has the columns the CDP-path "
             "reads depend on (run after an emdash update)",
    )
    verify_parser.add_argument("--config", required=True)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    command = args.command or "run"

    if command == "verify-emdash":
        if not args.config:
            parser.error("verify-emdash requires --config")
        raise SystemExit(verify_emdash(Path(args.config)))

    # command == "run" (explicit "run" subcommand, or the bare/default invocation)
    if not args.config:
        parser.error("--config is required")
    cfg = Config.load(Path(args.config))
    client = Client(cfg.base_url, cfg.token)
    if getattr(args, "drain_one", False):
        print(drain_one(cfg, client))
        return
    if args.once:
        action = run_once(cfg, client)
        _drain_chat_bridges(cfg, client)  # one-shot: don't exit mid-reply
        print(action)
        return

    # Startup banner — the log opens with exactly what this runner is configured to
    # do, so `~/.canopy/runner.log` is self-explaining.
    try:
        from .cdp_control import host_id
        host = host_id()
    except Exception:  # noqa: BLE001
        host = "?"
    logger.info("canopy-runner starting | runner=%s host=%s cdp_port=%s",
                cfg.runner_id, host, cfg.cdp_port)
    logger.info("  poll: claim every %ss | inbox every %ss | mailboxes=%s",
                cfg.poll_seconds, cfg.inbox_poll_seconds,
                ",".join(sorted(getattr(cfg, "mailboxes", {}))) or "(none)")
    logger.info("  COST note: idle cycles + inbox polls are ~free (HTTP only); a 'CREATE' "
                "line = one NEW claude session (tokens), 'REUSE' = none. grep the log for CREATE.")

    # Pause sentinel: the menu-bar app (or `touch ~/.canopy/PAUSED`) drops this file
    # to halt ALL token-spending work instantly without killing the process or fighting
    # launchd's KeepAlive. Paused = we still heartbeat (so the control plane sees the
    # runner alive-but-idle, not dead) but claim nothing, poll no inbox, spawn nothing.
    pause_file = Path(args.config).with_name("PAUSED")
    logger.info("  pause: drop %s to halt work (menu-bar app toggles this); remove to resume",
                pause_file)

    # Liveness heartbeat file: touched EVERY cycle (even idle/paused). The menu-bar app
    # reads its mtime to tell "running" from "stale" — the log alone is a bad signal
    # because idle cycles are deliberately quiet (~15 min between lines), which would
    # otherwise show a healthy idle runner as "stale".
    hb_file = Path(args.config).with_name("heartbeat")

    def _beat() -> None:
        try:
            hb_file.write_text(str(time.time()))
        except OSError:
            pass

    # RC3: a WS wake-listener lets the loop claim the INSTANT a turn is enqueued
    # instead of waiting out poll_seconds. Additive + best-effort — polling stays the
    # fallback and still owns heartbeat/claim/execute; off if websocket-client is absent.
    from .wake import WakeListener

    def _on_control(msg: dict) -> None:
        if msg.get("type") == "cancel" and msg.get("turn_id"):
            CANCELLED_TURNS.add(str(msg["turn_id"]))
        elif msg.get("type") == "menu_answer" and msg.get("session_key"):
            # A human answered, from the web, the dialog an agent is blocked on.
            # Runs on the wake-listener thread and must never raise: this socket
            # also carries cancel and wake, and losing it would cost the runner
            # its liveness for a keystroke.
            try:
                _answer_menu(str(msg["session_key"]), msg.get("option"),
                             cdp_port=cfg.cdp_port)
            except Exception:  # noqa: BLE001
                logger.warning("menu answer failed for %s", msg.get("session_key"),
                               exc_info=True)

    waker = WakeListener(cfg.base_url, cfg.token, cfg.runner_id, on_control=_on_control)
    wake_on = waker.start()
    if wake_on:
        logger.info("  wake: WS control channel connected — claims fire on enqueue, not just poll")
    _start_hook_listener(cfg, client)

    def _wait(seconds: float) -> None:
        # With a live wake channel, block until a nudge OR the poll interval,
        # whichever comes first. Without one (websocket-client absent — the
        # poll-only laptop, the cloud REST fallback, the test env), fall back to
        # the exact prior behavior: a plain time.sleep. Routing the wait through
        # the Event unconditionally would swallow the time.sleep the loop tests
        # patch to break the loop — an infinite hang.
        if not wake_on:
            time.sleep(seconds)
            return
        if waker.event.wait(seconds):  # returns early on a wake nudge
            waker.event.clear()

    idle_streak = 0
    paused = False
    while True:
        _beat()
        if pause_file.exists():
            if not paused:
                logger.warning("PAUSED — sentinel %s present; skipping all work (no claim, no "
                               "inbox, no tokens). Resume via the menu-bar app or remove the file.",
                               pause_file)
                paused = True
            try:
                client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT),
                                 note="paused", host=host,
                                 code_branch=_code_branch())
            except Exception:  # noqa: BLE001
                pass
            # Pause stops STARTING work, it doesn't abandon work already running: a
            # reply mid-flight when the sentinel dropped still gets carried back and
            # its turn closed, instead of hanging EXECUTING until the pause lifts.
            _pump_chat_bridges(cfg, client)
            time.sleep(cfg.poll_seconds)
            continue
        if paused:
            logger.info("RESUMED — pause sentinel cleared; back to normal polling")
            paused = False
            idle_streak = 0
        try:
            result = run_once(cfg, client)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("run_once crashed; continuing")
            result = "crashed"
        # One scannable line per cycle. Idle is quiet (a heartbeat every ~15 min so the
        # log shows the runner is alive without flooding); everything else logs at INFO.
        # "cdp_down" is quiet like "idle" — the throttled WARNING in run_once and the
        # degraded heartbeat already carry the reason, so logging it every tick would be the
        # per-tick spam the preflight is meant to avoid.
        if result in ("idle", "cdp_down"):
            idle_streak += 1
            if idle_streak % max(1, (900 // max(cfg.poll_seconds, 1))) == 0:
                logger.info("cycle: %s (x%d) — runner alive, nothing claimed", result, idle_streak)
        else:
            if idle_streak:
                logger.info("cycle: %s (after %d idle)", result, idle_streak)
            else:
                logger.info("cycle: %s", result)
            idle_streak = 0
        _wait(cfg.poll_seconds)  # wake-aware: claims fire on enqueue, not just poll


if __name__ == "__main__":
    main()
