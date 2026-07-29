"""Native macOS dialog for runner↔human collision resolution.

When the runner goes to deliver a turn into a live emdash session and finds the
prompt already holds unsent text — almost always the human's own words, leaked in
when emdash switched to that task while they were typing elsewhere — it asks the
human where to send instead of clobbering the line.

Renders via `osascript display dialog`, which only works when the runner runs in
the user's Aqua GUI session (a launchd **LaunchAgent**, which
`com.canopy.runner` is — not a LaunchDaemon). If osascript is unavailable or the
dialog times out with nobody answering, the choice comes back as NEW — the
non-destructive default (route to a fresh session, leave the existing prompt
untouched). We NEVER delete the human's text without an explicit "Clear & send".
"""
from __future__ import annotations

import subprocess

# The three button labels — also the return values. Kept identical to the AppleScript
# button strings below so a returned label round-trips by equality.
CLEAR = "Clear & send"
NEW = "New session"
CANCEL = "Cancel"

# argv: item 1 = message, item 2 = timeout seconds. Any error (no GUI session,
# osascript quirk) OR "gave up" (timed out) resolves to the safe default: New session.
_APPLESCRIPT = """on run argv
    set theMsg to item 1 of argv
    set theTimeout to (item 2 of argv) as integer
    try
        set r to display dialog theMsg with title "canopy runner — session busy" ¬
            buttons {"Cancel", "New session", "Clear & send"} ¬
            default button "Clear & send" giving up after theTimeout
    on error
        return "New session"
    end try
    if gave up of r then return "New session"
    return button returned of r
end run"""


def collision_choice(task: str, line: str, *, timeout: int = 30) -> str:
    """Ask the human where to deliver when session `task`'s prompt already has text.

    Returns one of CLEAR / NEW / CANCEL. Falls back to NEW on any error or timeout —
    the existing prompt is never destroyed without an explicit human Clear."""
    preview = (line or "").strip()
    if len(preview) > 120:
        preview = preview[:117] + "…"
    msg = (
        f'Session "{task}" already has unsent text in its prompt:\n\n'
        f"“{preview}”\n\n"
        f"Where should the agent's message go?\n\n"
        f"•  Clear & send — delete that text, send here\n"
        f"•  New session — leave it, send to a fresh session\n"
        f"•  Cancel — do nothing (retry later)"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-", msg, str(timeout)],
            input=_APPLESCRIPT,
            capture_output=True,
            text=True,
            timeout=timeout + 10,   # osascript self-times-out at `timeout`; this is a backstop
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return NEW
    choice = (proc.stdout or "").strip()
    return choice if choice in (CLEAR, NEW, CANCEL) else NEW
