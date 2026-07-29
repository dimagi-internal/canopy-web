"""CDP execution — the sanctioned path (supersedes DB injection + app patching).

The runner owns the harness Turn *routing* lifecycle (start → done) and drives
emdash over CDP to either REUSE an existing session (continuity) or CREATE a fresh
one (rehydrating context from the durable SessionLink). The WORK then happens in
the visible, intervenable emdash session.

Session reuse is verify-before-reuse, in two steps with two different authorities:

1. The control plane proposes reuse only when THIS runner's macOS host owns the live
   session (emdash is per-macOS-account; see SessionLink).
2. emdash's OWN sqlite decides whether that session still exists — `emdash.task_state`,
   a pure read. The DOM cannot answer this: the sidebar is virtualized, so an off-screen
   task looks identical to a deleted one, and believing that duplicated live sessions.

Genuinely gone (archived/absent) → create + rehydrate. Present but undriveable → FAIL
the turn; a duplicate is worse than a retry, because it orphans the live context.
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import re
import time
from pathlib import Path

from . import cdp_control, chat_bridge, dialog, emdash, readiness, transcript
from .tail import TailReader

logger = logging.getLogger("canopy_runner.execute")


def _target(turn: dict) -> str:
    """The emdash project to drive — an agent's slug or a repo's name. The CDP
    layer underneath takes a project name either way; this is the one place the
    two turn kinds converge."""
    return turn.get("agent_slug") or turn.get("project") or ""


def _thread_key(turn: dict) -> str:
    """The session-continuity key: which emdash session this turn continues.

    An explicit `thread_key`/`thread_id` in the origin_ref means CONTINUE the same
    session across turns — the phone's persistent per-target thread, a "continue this
    session" dispatch. With neither, the turn is a self-contained unit of work (a cron
    fire, a board turn) and must open its OWN fresh session, so we key it on the turn's
    unique id. The old `{agent}:main` fallback was a shared sink: every keyless turn —
    all of an agent's cron fires, every board dispatch — resolved to it and reused one
    ever-growing session (the `{agent}-cron-main-…` task). Continuity is opt-in; keyless
    is fresh-per-turn."""
    ref = turn.get("origin_ref") or {}
    explicit = ref.get("thread_key") or ref.get("thread_id")
    return explicit or f"{_target(turn)}:{turn.get('id') or ''}"


def _slug(text: str, n: int = 28) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n].strip("-")


def _task_name(agent: str, turn: dict, now=None) -> str:
    """A HUMAN-READABLE, per-thread-UNIQUE emdash task name: agent + subject slug +
    a short thread discriminator + MMDD-HHMM, e.g. 'hal-security-alert-6355-0714-1514'.
    The discriminator (last chars of the thread key) is what stops two DIFFERENT threads
    with the same subject in the same minute from colliding onto one name. Recorded in the
    SessionLink, so reuse opens this exact name — legible, not a bare hash."""
    stamp = (now or dt.datetime.now()).strftime("%m%d-%H%M")
    ref = turn.get("origin_ref") or {}
    label = _slug(ref.get("subject") or "") or _slug(turn.get("origin") or "")
    disc = re.sub(r"[^a-z0-9]", "", _thread_key(turn).lower())[-4:]
    bits = [agent] + ([label] if label else []) + [b for b in (disc, stamp) if b]
    return "-".join(bits)


def _deliver_to_existing(cfg, client, runner_id, turn, task, state, work_prompt):
    """Deliver a turn into the linked LIVE emdash session `task`.

    Returns a terminal action string (``reused:`` / ``failed:`` / ``deferred:``) when the
    turn is fully handled, or ``None`` to tell the caller to route to a NEW session
    (reuse fell back, or the human/timeout chose a fresh session on a collision).

    The collision case: `open_and_send` finds unsent text already in the prompt (the
    human's keystrokes leaked in when emdash switched to this task) and returns without
    clobbering it. We ask the human via a native dialog:
      Clear & send → kill the line, send here (``reused:``)
      New session  → leave it, create fresh (return ``None`` → caller creates)
      Cancel       → deliver nothing, requeue the turn (``deferred:``)
      timeout/no-GUI → treated as New session (non-destructive default).
    """
    turn_id = turn["id"]
    agent = _target(turn)
    thread_key = _thread_key(turn)

    try:
        res = cdp_control.open_and_send(task, work_prompt, port=cfg.cdp_port)
    except cdp_control.CDPError as exc:
        if state == "unknown" and "TASK_NOT_FOUND" in str(exc):
            # Degraded: emdash's DB was unreadable, so CDP's verdict is all we have.
            # Loud, because reuse is running blind until the db path is fixed.
            logger.warning("reuse: emdash DB unreadable — trusting CDP's TASK_NOT_FOUND "
                           "for '%s' (agent=%s); fix the db path to make reuse deterministic",
                           task, agent)
            client.post_events(turn_id, [{"kind": "status",
                "payload": {"status": "reuse_task_gone", "task": task, "task_state": state}}])
            return None                         # create + rehydrate
        # The task EXISTS (sqlite says so) but we couldn't drive it. Creating a fresh
        # session here would DUPLICATE the live one and orphan its context — the bug that
        # spawned two Hal sessions. Fail the turn instead (it retries); never duplicate.
        logger.error("reuse FAILED on '%s' which emdash's DB says is %s (agent=%s): %s "
                     "— NOT creating a duplicate; failing the turn for retry",
                     task, state, agent, str(exc)[:200])
        client.post_events(turn_id, [{"kind": "error",
            "payload": {"status": "reuse_send_failed", "task": task,
                        "task_state": state, "detail": str(exc)[:300]}}])
        _note = (f"reuse failed on existing session '{task}' ({state} per emdash's DB) "
                 f"— not spawning a duplicate; retry")
        readiness.mark_failed(cfg, _note)
        client.fail_turn(turn_id, _note)
        return f"failed:{turn_id}"

    if res.get("action") == "collision":
        choice = dialog.collision_choice(task, res.get("line", ""))
        logger.info("collision on '%s' (turn=%s): unsent text in prompt — human chose %r",
                    task, turn_id, choice)
        client.post_events(turn_id, [{"kind": "status",
            "payload": {"status": "collision", "task": task, "choice": choice}}])
        if choice == dialog.NEW:
            return None                         # leave the prompt untouched; create fresh
        if choice == dialog.CANCEL:
            _note = f"collision on session '{task}': cancelled by human; will retry"
            readiness.mark_ok(cfg)              # not a runner fault — a human deferral
            client.fail_turn(turn_id, _note)
            return f"deferred:{turn_id}"
        # CLEAR & send: kill the leaked text, then send into the same session.
        try:
            cdp_control.open_and_send(task, work_prompt, clear_first=True, port=cfg.cdp_port)
        except cdp_control.CDPError as exc:
            logger.error("collision clear-and-send failed on '%s': %s", task, str(exc)[:200])
            _note = f"collision clear-and-send failed on '{task}'; retry"
            readiness.mark_failed(cfg, _note)
            client.fail_turn(turn_id, _note)
            return f"failed:{turn_id}"
        # fall through to the shared success tail below

    # Delivered — either the empty-line fast path, or a cleared-then-sent collision.
    logger.info("REUSE  turn=%s agent=%s thread=%s -> existing session '%s' (no new claude session)",
                turn_id, agent, thread_key, task)
    client.post_events(turn_id, [{"kind": "status",
        "payload": {"status": "reused_session", "task": task, "thread_key": thread_key}}])
    client.record_session(runner_id, turn.get("agent_slug") or "", thread_key,
                          project=turn.get("project") or "",
                          workspace=turn.get("workspace_slug") or "", emdash_task_id=task)
    readiness.mark_ok(cfg)
    client.finish(turn_id, note=f"delivered to existing session '{task}'")
    return f"reused:{turn_id}"


def _resolve_transcript_path(target: str, task: str):
    home = Path.home()
    return transcript.resolve_transcript(
        target, task, home=home, claude_home=home / ".claude" / "projects"
    )


def _wait_for_transcript(target: str, task: str, *, timeout: float = 45.0, poll: float = 0.5):
    """A freshly-created emdash session's transcript .jsonl doesn't exist until Claude
    Code starts writing. Wait (bounded) for it to appear; reused sessions resolve at once."""
    deadline = time.monotonic() + timeout
    path = _resolve_transcript_path(target, task)
    while path is None and time.monotonic() < deadline:
        time.sleep(poll)
        path = _resolve_transcript_path(target, task)
    return path



# Where downloaded chat attachments land. Outside any repo checkout on purpose:
# the runner drives many projects and a screenshot is not source, so it must not
# appear as an untracked file in whichever repo the agent happens to be in.
ATTACHMENT_ROOT = pathlib.Path.home() / ".canopy" / "attachments"


def _safe_name(name: str) -> str:
    """A filename safe to join onto a local path. The server sanitises on upload
    too; this repeats it because a runner must never trust a path it was handed
    over the wire — `../../.ssh/authorized_keys` is a filename as far as JSON is
    concerned."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in name).strip(".-")
    return cleaned[:120] or "attachment"


def fetch_attachments(client, turn: dict) -> list[pathlib.Path]:
    """Download this turn's attachments; return the local paths, newest last.

    Best-effort per file: one unreachable attachment must not sink the whole
    turn, because the human's TEXT is usually the substance and a silently
    dropped image is better than a chat that never answers. What is not
    acceptable is dropping it silently from the agent's view, so the prompt only
    ever cites files that actually made it to disk.
    """
    refs = (turn.get("origin_ref") or {}).get("attachments") or []
    paths: list[pathlib.Path] = []
    for ref in refs:
        aid = ref.get("id")
        if not aid:
            continue
        dest = ATTACHMENT_ROOT / str(turn.get("id", "unknown")) / _safe_name(ref.get("filename", ""))
        try:
            client.download_attachment(aid, dest)
        except Exception:  # noqa: BLE001 — see docstring
            logger.warning("attachment %s could not be fetched; continuing without it", aid, exc_info=True)
            continue
        paths.append(dest)
    return paths


def prompt_with_attachments(prompt: str, paths: list[pathlib.Path]) -> str:
    """Put the files in front of the agent.

    Absolute paths, and an explicit instruction to read them: an agent that is
    merely TOLD a file exists usually keeps talking, whereas one told to read it
    reaches for the tool. The whole point of the feature is the agent actually
    looking at the screenshot.
    """
    if not paths:
        return prompt
    lines = "\n".join(f"- {p}" for p in paths)
    noun = "file" if len(paths) == 1 else "files"
    return (
        f"{prompt}\n\n"
        f"The user attached the following {noun}. Read {'it' if len(paths) == 1 else 'them'} "
        f"with the Read tool before replying:\n{lines}"
    )

def execute_chat_turn(cfg, client, runner_id: str, turn: dict, cancel_check=None) -> str:
    """A chat SESSION turn: inject the human's message into the session's emdash session,
    and REGISTER a bridge that carries the assistant reply back into the ledger — unlike
    agent/project turns, which fire-and-continue in the visible emdash session. The chat
    SessionConsumer turns the bridged assistant events into chat.stream_* so the website
    streams the reply.

    This function STARTS the turn, it does not wait for it. The reply is pumped tick by
    tick (`chat_pump.pump_chat_bridges`) and the turn is finished there, when the transcript
    says the agent handed the floor back. Cancellation moved with it: the pump watches
    CANCELLED_TURNS, presses Escape in the live emdash session, and finishes CANCELLED.
    `cancel_check` is accepted for signature compatibility and is no longer read here.
    """
    turn_id = turn["id"]
    agent_slug = turn.get("agent_slug") or ""
    project = turn.get("project") or ""
    workspace = turn.get("workspace_slug") or ""
    target = agent_slug or project  # the emdash project to drive
    thread_key = _thread_key(turn)
    prompt = turn.get("prompt") or ""
    prompt = prompt_with_attachments(prompt, fetch_attachments(client, turn))

    plan = client.resolve_session(
        runner_id, agent_slug, thread_key, project=project, workspace=workspace
    )
    client.start(turn_id)

    task = plan.get("emdash_task_id") if plan.get("reuse") else None
    if task and emdash.task_state(cfg.emdash_db, task) in ("absent", "archived"):
        task = None  # the linked emdash session is gone — create a fresh one
    if task:
        try:
            res = cdp_control.open_and_send(task, prompt, port=cfg.cdp_port)
        except Exception as exc:  # noqa: BLE001 — any send failure ends the turn
            logger.error("chat reuse send failed turn=%s task=%s: %s", turn_id, task, exc)
            client.fail_turn(turn_id, f"chat reuse send failed: {str(exc)[:200]}")
            return f"failed:{turn_id}"
        # A collision is `ok: true` with action="collision" and NOTHING delivered —
        # not an exception. This path used to discard the result entirely, so a
        # chat send into a prompt the human was typing in reported success while
        # the message went nowhere. The agent path has asked since #320; chat now
        # asks the same way, because it is the SAME hazard and the human is right
        # there.
        if res.get("action") == "collision":
            choice = dialog.collision_choice(task, res.get("line", ""))
            logger.info("chat collision on '%s' (turn=%s): unsent text in prompt — "
                        "human chose %r", task, turn_id, choice)
            if choice == dialog.CLEAR:
                cdp_control.open_and_send(task, prompt, clear_first=True, port=cfg.cdp_port)
            elif choice == dialog.CANCEL:
                # Deliver nothing and let the turn be retried, rather than finishing
                # a turn whose message never arrived.
                client.fail_turn(turn_id, f"chat send cancelled by human (collision on '{task}')")
                return f"cancelled:{turn_id}"
            else:
                # NEW (and the timeout default): never destroy what the human typed.
                # A fresh session is wrong for a CHAT — the conversation lives in this
                # one — so the honest outcome is to leave it and retry.
                client.fail_turn(turn_id, f"chat send deferred: unsent text in '{task}'")
                return f"deferred:{turn_id}"
        logger.info("chat turn=%s reused emdash task=%s (agent=%s)", turn_id, task, target)
    else:
        try:
            res = cdp_control.create_task(
                target, prompt, task_name=_task_name(target, turn), port=cfg.cdp_port
            )
        except cdp_control.CDPError as exc:
            logger.error("chat create failed turn=%s agent=%s: %s", turn_id, target, exc)
            client.fail_turn(turn_id, f"chat create failed: {str(exc)[:200]}")
            return f"failed:{turn_id}"
        task = res.get("task") or ""
        client.record_session(
            runner_id, agent_slug, thread_key, project=project, workspace=workspace,
            emdash_task_id=task, summary=None,
        )
        logger.info("chat turn=%s created emdash task=%s (agent=%s)", turn_id, task, target)

    path = _wait_for_transcript(target, task)
    if path is None:
        logger.warning("chat turn=%s: no transcript for task=%s (agent=%s) — reply not bridged",
                       turn_id, task, target)
        client.finish(turn_id, note="chat: transcript not found; reply not bridged")
        return f"chat:{turn_id}:{task}"
    # Hand off to the tick pump and RETURN — the turn stays EXECUTING until the agent
    # hands the floor back. Waiting here would block the whole runner loop (heartbeat,
    # claims, session reports) for the length of an agent turn, which is minutes.
    reader = TailReader(str(path))
    reader.seek_end()  # byte-equivalent of the old start_index snapshot, without re-reading
    chat_bridge.IN_FLIGHT[turn_id] = chat_bridge.LiveBridge(
        turn_id=turn_id, task=task, reader=reader,
    )
    logger.info("chat turn=%s bridging from task=%s (agent=%s)", turn_id, task, target)
    return f"chat:{turn_id}:{task}"


def execute_turn(cfg, client, runner_id: str, turn: dict, cancel_check=None) -> str:
    """Route one claimed turn to an emdash session (reuse or create). Returns a short
    action string: reused:<id> | created:<id>:<task> | failed:<id>.

    `cancel_check` (`lambda turn_id: bool`) is threaded straight through to
    `execute_chat_turn` — only chat/session turns are cancellable today (see there)."""
    # A chat session send is bridged back to the website (its own path); everything
    # else fires into the visible emdash session and continues there.
    if (turn.get("origin_ref") or {}).get("chat_session_id"):
        return execute_chat_turn(cfg, client, runner_id, turn, cancel_check=cancel_check)
    turn_id = turn["id"]
    # `agent` names the emdash project to drive for BOTH turn kinds — an agent's
    # slug or, for a repo turn, the project name. cdp_control takes a project name
    # either way, so the executor below is unchanged; only the session-link calls
    # need to know which kind it is (a project link is tenant-gated on workspace).
    agent_slug = turn.get("agent_slug") or ""
    project = turn.get("project") or ""
    workspace = turn.get("workspace_slug") or ""
    agent = agent_slug or project  # the CDP/emdash target
    ref = turn.get("origin_ref") or {}
    thread_key = _thread_key(turn)
    # The prompt is a clean command — the agent's namespaced /<slug>:turn does all the work.
    # Default (board turns with no prompt): a full turn. drain-turn is retired.
    # A repo turn always carries an explicit prompt (the composer requires it).
    work_prompt = turn.get("prompt") or f"/{agent}:turn"

    plan = client.resolve_session(
        runner_id, agent_slug, thread_key, project=project, workspace=workspace
    )
    client.start(turn_id)
    # Log the plan: "why did it create a new session?" must be answerable from the log
    # alone. Without this the reuse decision was invisible and every diagnosis started
    # by guessing (see the 2026-07-15 eva org-research investigation).
    logger.info("resolve turn=%s agent=%s thread=%s -> reuse=%s task=%r link=%s",
                turn_id, agent, thread_key, plan.get("reuse"),
                plan.get("emdash_task_id") or "", plan.get("link_id"))

    # --- reuse the existing session, if the control plane says this runner owns it ---
    if plan.get("reuse") and plan.get("emdash_task_id"):
        task = plan["emdash_task_id"]
        # sqlite — NOT the DOM — decides whether the session still exists. emdash
        # virtualizes its sidebar, so "not in the page" never meant "not real"; believing
        # it duplicated live sessions. See emdash.task_state. "unknown" = no truth
        # available, so we defer to CDP rather than wedge every turn.
        state = emdash.task_state(cfg.emdash_db, task)
        if state in ("absent", "archived"):
            logger.warning("reuse: task '%s' is %s per emdash's DB (agent=%s) — creating "
                           "fresh + rehydrating", task, state, agent)
            client.post_events(turn_id, [{"kind": "status",
                "payload": {"status": "reuse_task_gone", "task": task, "task_state": state}}])
        else:
            # Try to deliver into the live session (handles the reuse-glitch and the
            # human-was-typing collision). A terminal outcome ends the turn here; None
            # means "route to a fresh session" and falls through to create below.
            outcome = _deliver_to_existing(cfg, client, runner_id, turn, task, state, work_prompt)
            if outcome is not None:
                return outcome

    # --- create a fresh session, rehydrating durable context when we have it ---
    prompt = work_prompt
    summary = plan.get("summary") or ""
    if summary:
        prompt = (f"[Continuing prior work on this thread — context from earlier sessions "
                  f"(a fresh session, possibly a different machine):]\n{summary}\n\n{work_prompt}")
    if plan.get("reuse"):
        # We got here from a reuse that fell back — worth flagging: a persistently
        # failing reuse means a NEW session per turn (cost). Investigate the task.
        logger.warning("REUSE FELL BACK to CREATE for thread=%s (agent=%s) — the linked "
                       "emdash session was unreachable; check for a stuck/gone task", thread_key, agent)
    try:
        res = cdp_control.create_task(agent, prompt, task_name=_task_name(agent, turn), port=cfg.cdp_port)
    except cdp_control.CDPError as exc:
        logger.error("CREATE failed turn=%s agent=%s: %s", turn_id, agent, exc)
        _note = f"emdash create failed: {exc}"
        readiness.mark_failed(cfg, _note)
        client.fail_turn(turn_id, _note)
        return f"failed:{turn_id}"

    task = res.get("task") or ""
    logger.info("CREATE turn=%s agent=%s thread=%s -> new session '%s' rehydrated=%s "
                "(NEW claude session = tokens)", turn_id, agent, thread_key, task, bool(summary))
    client.post_events(turn_id, [{"kind": "status",
        "payload": {"status": "created_session", "task": task, "thread_key": thread_key,
                    "rehydrated": bool(summary)}}])
    client.record_session(
        runner_id, agent_slug, thread_key, project=project, workspace=workspace,
        emdash_task_id=task,
        agent_task_ext_id=ref.get("agent_task_ext_id"),
        summary=summary or None,
    )
    readiness.mark_ok(cfg)
    client.finish(turn_id, note=f"created session '{task}'" + (" (rehydrated)" if summary else ""))
    return f"created:{turn_id}:{task}"
