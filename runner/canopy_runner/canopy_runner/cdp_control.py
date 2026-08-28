"""Python wrapper over the emdash CDP control sidecar (Node + playwright-core).

The runner drives emdash through its real UI over CDP — the sanctioned path that
supersedes DB injection + app patching. This module shells out to
`cdp/emdash_control.mjs`; keep the Python side thin. One-time setup:
`cd canopy_runner/cdp && npm install`.
"""
from __future__ import annotations

import getpass
import json
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

SIDECAR = Path(__file__).parent / "cdp" / "emdash_control.mjs"
# The one dependency emdash_control.mjs imports. Its presence is the cheap,
# specific probe for "have this sidecar's node deps been installed here?" —
# a bare `node_modules/` exists() would pass on a half-finished install.
_SIDECAR_DEP = "playwright-core"


class CDPError(Exception):
    """emdash CDP control failed — often "task not present" (reuse should fall back
    to create) or "cannot connect" (emdash not launched with the debug port)."""


def sidecar_deps_installed() -> bool:
    return (SIDECAR.parent / "node_modules" / _SIDECAR_DEP).exists()


def ensure_sidecar_deps(*, timeout: int = 300) -> None:
    """Install the sidecar's node deps NEXT TO the sidecar, once, if missing.

    The `.mjs` ships inside the wheel (it is code, versioned with the Python that
    calls it), but `node_modules` cannot: `playwright-core` is a Node dependency,
    and site-packages is replaced wholesale on every reinstall. So a freshly
    installed runner has the sidecar and not its deps, and would fail at the first
    CDP call with a Node resolution error naming a path the user has never seen.

    Deps must live NEXT TO the sidecar rather than in a stable shared directory:
    `NODE_PATH` is consulted for CommonJS resolution only, and this sidecar is
    `type: module`, so Node resolves its bare import by walking up from the .mjs.

    Idempotent and cheap when already present (one `exists()`). Called at daemon
    STARTUP and by `canopy-runner install-sidecar` (which `install-runner.sh` runs,
    so the cost lands on the install rather than on the first turn of the day) —
    deliberately NOT lazily from `_run`: a hot path that can shell out to npm is
    both a surprising mid-turn latency spike and something every CDP unit test
    would have to defend against.
    """
    if sidecar_deps_installed():
        return
    try:
        proc = subprocess.run(
            ["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=str(SIDECAR.parent), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CDPError(
            f"npm not found — the CDP sidecar's deps are missing at {SIDECAR.parent} "
            f"and cannot be installed. Install Node.js, then re-run "
            f"`canopy-runner install-sidecar`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CDPError(f"npm install for the CDP sidecar timed out after {timeout}s") from exc
    if proc.returncode != 0 or not sidecar_deps_installed():
        raise CDPError(
            f"npm install for the CDP sidecar failed in {SIDECAR.parent}: "
            f"{(proc.stderr or proc.stdout or '')[:300]!r}"
        )


HOST_ID_PATH = Path.home() / ".canopy" / "host-id"


def host_id() -> str:
    """The ownership key deciding whether a live emdash session is reusable — pinned on
    first use, because it MUST be stable and macOS's hostname is not.

    `socket.gethostname()` flaps between the Bonjour and DHCP names (observed
    2026-07-15: Jonathans-MacBook-Pro.local <-> Jonathans-MBP.localdomain, three
    restarts each way in a day). SessionLink.reusable_by() compares this value by string
    EQUALITY, so every flap silently orphaned every link recorded under the other name:
    resolve returned reuse=false, each thread got a fresh cold session, and nothing was
    logged anywhere. Proved by experiment — one restart flipped the same live link from
    reuse=true to reuse=false with nothing else changed.

    So pin the FIRST value computed and reuse it forever. Still human-readable in the
    runner list (unlike a raw UUID), but stable. The pin lives under the account's own
    home, which is exactly the ownership semantic emdash needs: sessions are
    per-macOS-account, so two accounts get two ids and one account always gets one.

    Pre-existing links recorded under the other name self-heal: one create each, then
    stable. An unwritable pin degrades to the live value — flappy, but no worse.
    """
    try:
        pinned = HOST_ID_PATH.read_text().strip()
        if pinned:
            return pinned
    except OSError:
        pass                    # not pinned yet (or unreadable) — compute and try to pin
    current = f"{getpass.getuser()}@{socket.gethostname()}"
    try:
        HOST_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        HOST_ID_PATH.write_text(current + "\n")
    except OSError:
        pass                    # unwritable — degrade rather than refuse to heartbeat
    return current


def cdp_healthy(*, port: int = 9222, timeout: float = 1.0) -> bool:
    """True iff emdash's CDP endpoint answers on `port` — a short-timeout preflight the
    runner runs BEFORE claiming a turn, so a down emdash skips the claim instead of
    claiming-then-failing (which burns the turn: a failed turn is not auto-re-claimed).

    Probes DevTools' ``/json/version`` — the same endpoint playwright's connectOverCDP
    hits — so a green probe means create/reuse will actually connect. Any failure
    (connection refused → emdash closed/crashed/rebooted, or launched without
    --remote-debugging-port; timeout; non-200) returns False. Never raises: this gates
    the loop, so it must fail closed (skip the claim) rather than crash the tick."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return getattr(resp, "status", 200) == 200
    except (urllib.error.URLError, OSError, ValueError):
        # URLError (refused/timeout), OSError (socket), ValueError (odd url) — all mean
        # "not reachable right now". TimeoutError is an OSError, so it's covered.
        return False


def _run(command: str, args: dict, *, node: str = "node", timeout: int = 90) -> dict:
    try:
        proc = subprocess.run(
            [node, str(SIDECAR), command, json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CDPError("node not found — install Node.js, then run "
                       "`canopy-runner install-sidecar`") from exc
    except subprocess.TimeoutExpired as exc:
        raise CDPError(f"emdash CDP '{command}' timed out after {timeout}s") from exc
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise CDPError(
            f"emdash CDP '{command}' returned non-JSON: {raw[:200]!r} "
            f"stderr={proc.stderr[:200]!r}"
        ) from exc
    if not data.get("ok"):
        raise CDPError(data.get("error") or proc.stderr.strip() or f"emdash CDP '{command}' failed")
    return data


def list_tasks(*, port: int = 9222) -> dict:
    """{tasks:[names], projects:[names]} currently visible in emdash."""
    return _run("list", {"port": port})


def probe(*, port: int = 9222) -> dict:
    """READ-ONLY: a count per DOM contract this module depends on.

    The upgrade counterpart to `verify-emdash`'s schema check, for the half of the
    coupling that lives in emdash's UI rather than its DB. Clicks nothing and opens
    nothing, so it is safe to run against a fleet mid-turn.

    Counts rather than booleans because the interesting failures are not binary: a
    sidebar that renders but exposes no `Open task` labels is a drift, and so is one
    that suddenly exposes two Claude tabs where `send-keys` requires exactly one.
    """
    return _run("probe", {"port": port}, timeout=30)


def create_task(project: str, prompt: str, *, task_name: str = "", port: int = 9222) -> dict:
    """Create a NEW emdash task under `project` with `prompt` as the initial message.
    Pass `task_name` for a deterministic, reusable name (recommended — the auto-name
    diff is unreliable under sidebar virtualization). Returns {..., "task": name}."""
    args = {"port": port, "project": project, "prompt": prompt}
    if task_name:
        args["taskName"] = task_name
    return _run("create", args)


def open_and_send(task: str, text: str, *, clear_first: bool = False, port: int = 9222) -> dict:
    """REUSE: open an existing task and deliver `text` into its live terminal.

    Returns the sidecar dict — normally ``{"action": "sent"}``. If the prompt already
    holds UNSENT text (the human was typing when emdash switched tasks and their
    keystrokes leaked in), returns ``{"action": "collision", "line": "<preview>"}``
    WITHOUT clobbering it — the caller asks the human what to do (see `dialog.py`) and
    may re-call with ``clear_first=True`` to kill the current line first, then send
    (``{"action": "sent-cleared"}``).

    Raises CDPError if the task isn't present (caller falls back to create+rehydrate),
    or with ``COMPOSER_NOT_VISIBLE`` if the rendered frame shows no input line
    (mid-redraw, a menu is up, or a stale frame) — the sidecar refuses a blind send
    it can't verify; the caller fails the turn for retry rather than duplicating."""
    args = {"port": port, "task": task, "text": text}
    if clear_first:
        args["clearFirst"] = True
    return _run("open-send", args)


def read_terminal(task: str, *, port: int = 9222) -> str:
    """The task's rendered terminal, as text.

    This is how canopy sees a dialog that exists only on screen. A hook can say
    an agent is blocked but never WHAT it is asking, and emdash owns the session,
    so the menu is only in the terminal.

    Reads the DOM, not the PTY: emdash's xterm uses the DOM renderer, so it has
    already resolved the TUI's cursor-movement escapes into real cells. The raw
    stream would need re-rendering (Claude Code draws spaces as ESC[nC, so
    stripping ANSI welds words together).
    """
    return _run("read-term", {"task": task, "port": port}).get("text") or ""


def send_keys(task: str, keys: list[str], *, port: int = 9222) -> dict:
    """Press `keys` in the task's terminal, one at a time.

    One at a time, not as inserted text: a menu answer must be exactly "3" then
    Enter. Inserting a string would type the digit into the PROMPT of a session
    that turned out not to be showing a menu.
    """
    return _run("send-keys", {"task": task, "keys": keys, "port": port})


def interrupt(task: str, *, port: int = 9222) -> dict:
    """Press Escape in the task's emdash session — interrupts the running turn.

    Opens `task` the same way `open_and_send` does (no text is inserted), then sends
    Escape, which Claude Code's TUI treats as "stop the current turn". Raises CDPError
    if the task isn't present (mirrors open_and_send's TASK_NOT_FOUND)."""
    return _run("interrupt", {"task": task, "port": port})


def close_task(task: str, *, port: int = 9222) -> dict:
    """DELETE `task` from emdash (the designed close behaviour). Returns {"action": "deleted"} or {"action": "absent"}.

    emdash's context menu offers both delete and archive; delete is the chosen close
    behaviour here, not the only option. It is not undoable in emdash. It is not
    destructive to the record: canopy keeps the Session, its Turns and their ledger,
    and Claude Code's transcript (under ~/.claude/projects, resolved by path and never
    deleted by Claude Code), so the conversation stays readable and re-derivable.

    "absent" is SUCCESS, not TASK_NOT_FOUND: a double-tap from the phone and a task
    a human just deleted both land here, and the desired state already holds. This is
    the opposite of `open_and_send`, where absence means "do not create a duplicate".

    The sidecar re-checks the sidebar before reporting "deleted". That verification
    is load-bearing: the server writes nothing when it relays a close, so a close we
    only attempted must never be reported as done.
    """
    return _run("close-task", {"task": task, "port": port})
