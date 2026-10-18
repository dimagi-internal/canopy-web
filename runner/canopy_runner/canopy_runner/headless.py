"""Running a Claude turn on this box, headless — the `claude-cli` runtime's executor.

The `claude-cli` session runtime (desktop.py) opens no app: the runner runs Claude
itself over ACP, and canopy-web is where the session is watched and steered. A
person who wants a session in the Claude desktop app opens it from the app's
resume picker; from then on the app's process owns it and follow-ups go in
through the canopy-desktop mod (desktop.py decides which).

The turn loop is the cloud runner's, shared: `canopy_acp.drive_turn` (events,
replay, wedge guard, waiting for the session's last process to exit). This module
only adds what a laptop needs around it — where the adapter lives, a stop from
another thread, and keeping the mod out.

ONE LIVE PROCESS PER SESSION. Two Claude processes resumed on one session each
work from the history they loaded at start: both append to the same transcript,
which forks, and a later resume follows one branch — the other's turns drop out
of its context, silently (measured 2026-10-18 on Claude Code 2.1.302). So the
adapter lives for ONE turn: between turns no process holds the session, which is
also what the app's resume picker needs (it refuses a running session).

THE MOD MUST NOT CLAIM A HEADLESS SESSION. The worktree enables the canopy-desktop
mod, so the headless process loads it too. Its env carries CANOPY_HEADLESS=1, and
the mod claims the channel (stamps `alive`, takes prompts) only where that is
unset — otherwise the runner would read its own turn as "the app owns this".
"""
from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path

logger = logging.getLogger("canopy_runner.headless")

#: Where a laptop runner installs the adapter: launchd's PATH is the bare system
#: one, so it is found by this prefix (canopy_acp.ADAPTER_PREFIXES) or not at all.
ADAPTER_PREFIX = Path.home() / ".canopy" / "acp"
#: Set in the headless process's env; the mod stays inert where it is set.
HEADLESS_ENV = "CANOPY_HEADLESS"
#: How often streamed events are pushed while a turn runs.
FLUSH_SECONDS = 3.0

#: session id -> the adapter running a turn in it, for stop and the session report.
RUNNING: dict[str, object] = {}
_lock = threading.Lock()


def adapter_argv() -> list[str] | None:
    """The argv that starts the ACP adapter, or None when it is not installed."""
    from canopy_acp import find_adapter

    return find_adapter()


def ensure_adapter() -> bool:
    """Install the adapter under ADAPTER_PREFIX if it is missing. True when present."""
    from canopy_acp import install_adapter

    outcome = install_adapter(ADAPTER_PREFIX)
    if outcome not in ("present", "installed"):
        logger.error("claude-cli runtime: the ACP adapter could not be installed (%s)", outcome)
    return adapter_argv() is not None


def is_running(sid: str) -> bool:
    with _lock:
        return sid in RUNNING


def cancel(sid: str) -> bool:
    """Cancel the turn running headless in `sid`. False when none is."""
    with _lock:
        agent = RUNNING.get(sid)
    if agent is None:
        return False
    agent.cancel()
    return True


def run_turn(prompt: str, *, cwd: Path, emit, env: dict | None = None,
             resume: str | None = None, on_session=None, cancelled=lambda: False,
             timeout: float = 4 * 3600) -> tuple[bool, str, str]:
    """Run one turn headless. Returns (ok, final text, session id); the text is
    "cancelled" for a cancelled turn. `on_session(sid)` runs once the session
    exists, before the prompt — with a NEW id if a resume could not load.
    Raises canopy_acp.TurnError when the turn could not run to an end."""
    import canopy_acp

    argv = adapter_argv()
    if argv is None:
        raise RuntimeError(f"the ACP adapter is not installed under {ADAPTER_PREFIX}; "
                           "`canopy-runner runtime set claude-cli` installs it")
    agents: list = []

    def started(sid: str) -> None:
        with _lock:
            RUNNING[sid] = agents[0]
        if on_session is not None:
            on_session(sid)

    def ended(agent) -> None:
        with _lock:
            for sid in [s for s, a in RUNNING.items() if a is agent]:
                RUNNING.pop(sid, None)

    res = canopy_acp.drive_turn(
        prompt, make_agent=functools.partial(canopy_acp.AcpAgent, argv=argv),
        reducer=canopy_acp.UpdateReducer(), emit=emit, cwd=cwd,
        env={**(env or {}), HEADLESS_ENV: "1"}, resume=resume,
        on_start=agents.append, on_session=started, on_end=ended, cancelled=cancelled,
        flush_seconds=FLUSH_SECONDS, timeout=timeout, log=logger.info)
    if res.cancelled:
        return False, "cancelled", res.session_id
    if res.rate_limit.get("status") == "rejected":
        return False, f"{res.text}\n\nYou've hit your usage limit", res.session_id
    return res.ok, res.text, res.session_id
