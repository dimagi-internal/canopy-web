"""The hook-driven half of a live session: tool events, and the dialog a blocked
agent is waiting on.

Claude Code fires a hook per tool call straight to a loopback listener this
runner owns — the LIVE half of a session's record (the transcript is complete
but lags, so it cannot drive a view you are actively watching).

Menu ANSWERING lives here rather than in `menu.py` because it is hook-shaped:
it resolves a cwd/session_key to an emdash task. `menu.py` stays the pure
parser, which is what keeps this dependency one-directional."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import canopy_transcript as transcript_core

from . import cdp_control, hook_install, menu
from .menu_store import MenuStore
from .client import Client
from .config import Config

logger = logging.getLogger("canopy_runner")


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


def maybe_report_hooks(now_fn=time.monotonic) -> None:
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


def ensure_hook_config(settings_path=None) -> bool:
    """Re-point the hook config at the live listener if it has drifted. Returns
    True if it was repaired.

    `install` runs ONCE, at startup — but `~/.claude/settings.json` is one file
    per ACCOUNT, shared by every runner instance and every Claude Code session on
    it, so whatever writes last wins permanently. The free-port fallback is
    itself such a writer: a second instance that loses the port race takes an
    ephemeral port and re-points the account's hooks at itself. When that
    instance exits, every hook on the account curls a port nothing is listening
    on — the live-events-off failure the fallback exists to prevent, arriving by
    a different door. Observed 2026-07-30: runner on :8788, config on :49366,
    nothing bound there.

    So the config is treated as observed state that converges, not as a fact
    declared once — the same shape as `capabilities["projects"]` (reported, never
    typed) and session liveness (polled, self-healing). One small read per tick
    buys an invariant that repairs itself within a poll no matter who wrote over
    it.

    Deliberately NOT folded into `maybe_report_hooks`: that returns early while
    `received == 0`, and "no hooks are arriving" is precisely the condition being
    healed, so the repair would be unreachable exactly when it is needed.
    """
    listener = _hook_listener
    if listener is None or listener.port <= 0:
        return False
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    if hook_install.is_current(path, port=listener.port, nonce=listener.nonce):
        return False
    if not hook_install.install(path, port=listener.port, nonce=listener.nonce):
        return False
    logger.warning(
        "hook config no longer pointed at this listener (something else wrote %s); "
        "re-pointed it at :%d", path, listener.port)
    return True


def resolve_hook_session(cwd: str) -> str:
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


def hook_project_task_keys(cwd: str, *, home: Path | None = None) -> list[tuple[str, str]]:
    """This cwd as the (project, emdash task) keys the session report looks up by.

    **Deliberately does NOT consult `_hook_sessions`.** That map is rebuilt
    wholesale from `sync_streams`, which returns only the sessions a VIEWER is
    attached to — so keying a menu off it would capture one exactly when somebody
    already had the chat open, and lose it in every case that matters. You go and
    look BECAUSE the session stopped; the menu has to be there before you arrive.
    That is the same shape as the bug #510 left behind, one layer down.

    Nothing here needs a canopy session id anyway: the report is keyed by
    (project, emdash task), and the worktree path carries both.

    Returns every candidate because the directory may carry emdash's random
    de-dupe suffix and the cwd alone cannot say which form the report uses. They
    are all derived from this one path, so at most one is ever queried — storing
    under each is a spelling, not an ambiguity.

    Still (project, task) and never the task alone: emdash task names are not
    unique across projects, and a menu attached to the wrong session puts
    somebody else's buttons on your phone.
    """
    if not cwd:
        return []
    parsed = transcript_core.parse_emdash_worktree(cwd, home=home or Path.home())
    if parsed is None:
        return []
    project, task = parsed
    return [(project, c) for c in transcript_core.emdash_task_candidates(task) if c]


def note_answer_outcome(session_key: str, outcome: str) -> None:
    """Record a tap's outcome against the menu it was aimed at.

    Keyed by emdash task name across every project, because a `menu_answer` frame
    carries only the session_key — and a menu is stored per (project, task). Task
    names are effectively unique within one box's open set; the alternative is
    threading the project through the control frame for a note that is only ever
    read beside the menu it is attached to.
    """
    listener = _hook_listener
    if listener is None or not session_key:
        return
    try:
        keys = [k for k in listener._pending_menus if k[1] == session_key]
        listener.note_answer(keys, outcome, ANSWER_NOTES.get(outcome, ""))
    except Exception:  # noqa: BLE001 — never cost the wake listener its socket
        logger.debug("could not record the answer outcome for %s", session_key, exc_info=True)


def pending_hook_menu(project: str, task: str):
    """The dialog this session is waiting on, per the hook listener, or None."""
    listener = _hook_listener
    if listener is None:
        return None
    try:
        return listener.pending_menu(project, task)
    except Exception:  # noqa: BLE001 — this runs inside the liveness report
        logger.debug("pending hook menu read failed for %s/%s", project, task, exc_info=True)
        return None


def hook_task_name(cwd: str) -> str:
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



def read_hook_menu_from(cdp, task: str, *, cdp_port: int = 9222):
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


# What became of a human's tap. Every one of these is reported back rather than
# logged and dropped: the server answers the phone `ok:true` the instant it
# relays the frame, so a refusal that stops here is indistinguishable from a
# press that worked — which IS the "clicking does nothing" bug, and it survived
# every fix to the causes underneath it.
ANSWERED = "answered"
NO_DIALOG = "no_dialog"          # nothing on screen — already answered, or gone
NOT_ON_MENU = "not_on_menu"      # stale numbering; the dialog changed under the tap
WRONG_PANE = "wrong_pane"        # a shell tab is selected; keys would run in it
UNREACHABLE = "unreachable"      # CDP/emdash could not be driven at all

# Human-readable, and shown on the phone beside the menu that did not move. Kept
# here rather than in the client so all three surfaces say the same thing.
ANSWER_NOTES = {
    NO_DIALOG: "That dialog is no longer on screen — it may already have been answered.",
    NOT_ON_MENU: "The dialog changed before that reached it. Here is what it shows now.",
    WRONG_PANE: "A shell tab is selected for this session in emdash. Switch it to the "
                "Claude tab and tap again.",
    UNREACHABLE: "Could not reach emdash on this runner to press the key.",
}


def answer_menu_with(cdp, session_key: str, option, *, cdp_port: int = 9222) -> str:
    """Press a human's answer into `session_key`'s terminal. Returns an outcome.

    Re-reads the screen FIRST and refuses an option that is not on it. A menu can
    go stale between the phone rendering it and a thumb reaching it — and a
    NUMBER typed at a session no longer showing a dialog lands in its prompt,
    where the agent reads a bare "1" as an instruction. Double-taps and two
    people answering at once both land here.

    The outcome is RETURNED, never merely logged. A refusal here is correct and
    also invisible: the API already told the phone `ok:true`, so silence reads as
    success and the human taps again, and again. Telling them what happened is
    the difference between a button that failed and a button that lied.
    """
    # Re-read with a settle: a single read can catch the TUI mid-render, and
    # dropping a human's tap because the footer had not painted yet is a bug
    # they experience as "the button did nothing".
    current = menu.find_menu_settled(
        lambda: cdp.read_terminal(session_key, port=cdp_port))
    if current is None:
        logger.info("menu answer for %s ignored — no dialog on screen now", session_key)
        return NO_DIALOG
    number = None if option is None else int(option)
    if not current.allows(number):
        logger.warning("menu answer %r for %s is not on the dialog now showing (%d options)",
                       number, session_key, len(current.options))
        return NOT_ON_MENU
    cdp.send_keys(session_key, menu.answer_keys(number), port=cdp_port)
    logger.info("answered the dialog on %s with %s", session_key,
                "Esc" if number is None else f"option {number}")
    return ANSWERED


def read_hook_menu(cwd: str, *, cdp_port: int):
    """The dialog on this session's screen, or None.

    Only called when a session reports BLOCKED: a hook can say an agent wants a
    human but never what it is asking, and emdash owns the session, so the
    question and its options exist only on the terminal.
    """
    return read_hook_menu_from(cdp_control, hook_task_name(cwd), cdp_port=cdp_port)


def answer_menu(session_key: str, option, *, cdp_port: int = 9222) -> str:
    """`answer_menu_with` bound to real CDP, with transport failures classified.

    Never raises: this runs on the wake-listener thread, which also carries wake
    and cancel, and losing that socket over one keystroke would cost the runner
    its liveness.
    """
    try:
        return answer_menu_with(cdp_control, session_key, option, cdp_port=cdp_port)
    except Exception as exc:  # noqa: BLE001
        # NOT_A_CLAUDE_PANE is the one refusal a human can act on themselves, and
        # it is the one that actually bit: with a shell tab selected, every tap on
        # a real dialog died here for 45 minutes with nothing said (labs,
        # 2026-08-01). It is deliberately still a refusal — clicking the Claude
        # tab for somebody would mean guessing at emdash's tab controls, and the
        # cost of guessing wrong is a digit executed in their shell.
        outcome = WRONG_PANE if "NOT_A_CLAUDE_PANE" in str(exc) else UNREACHABLE
        logger.warning("menu answer for %s failed (%s)", session_key, outcome, exc_info=True)
        return outcome



def start_hook_listener(cfg: Config, client: Client):
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
        resolve_session=resolve_hook_session,
        forward=lambda: cfg.forward_sessions,
        # NOT read_menu (see above) — this is the cheap half: `PreToolUse` for
        # AskUserQuestion already carries the whole dialog, so the listener can
        # hold it for the session report without touching emdash at all. And it
        # resolves off the cwd alone, so it covers every session on the box
        # rather than only the ones somebody is already watching.
        resolve_task=hook_project_task_keys,
        # Beside runner.json and the PAUSED sentinel — per-box state, not config.
        menu_store=MenuStore(Path.home() / ".canopy" / "pending-menus.json"),
    )
    listener.bind_sender(
        lambda session_id, events: client.post_session_stream(
            cfg.runner_id, session_id, events)
    )
    try:
        listener.start()
    except OSError as exc:
        # `start` already falls back to a free port when the configured one is
        # merely taken, so reaching here means the loopback socket itself is
        # unusable. Live events are an overlay, so it stays non-fatal — but it is
        # logged at ERROR, because the failure is silent everywhere else: the
        # chat UI just never says "running" again, which reads as the app
        # ignoring you rather than as a runner with a dead listener.
        logger.error("hook listener could not bind (%s); live events OFF — the chat "
                     "UI will not show agent activity for this runner's sessions", exc)
        return None
    # `listener.port`, not `cfg.hook_port`: the fallback above may have taken a
    # different one, and the hook config has to point where we are ACTUALLY
    # listening. Installing the configured port after binding another is how a
    # collision turns into hooks curling into a hole.
    hook_install.install(settings_path, port=listener.port, nonce=nonce)
    logger.info("live hook events: listener on :%d, forwarding=%s",
                listener.port, cfg.forward_sessions)
    _hook_listener = listener
    return listener

