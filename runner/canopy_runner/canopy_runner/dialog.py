"""Native dialog for runner↔human collision resolution — macOS AND Windows.

When the runner goes to deliver a turn into a live emdash session and finds the
prompt already holds unsent text — almost always the human's own words, leaked in
when emdash switched to that task while they were typing elsewhere — it asks the
human where to send instead of clobbering the line.

**This was macOS-only, and silently so.** `platform_jobs` says the rest of the
runner is platform-neutral and that supervision is the only OS-specific thing;
that was true of every module except this one. On Windows `osascript` raised
FileNotFoundError, the except arm returned NEW, and the human was never asked —
so a collision there did not degrade to "ask later", it degraded to a chat
message the sender watched vanish. That is worse than on macOS, where the dialog
at least appears if someone is at the machine. Two renderers now, one contract.

- **macOS** — `osascript display dialog`, three real buttons, which only works
  when the runner is in the user's Aqua GUI session (a launchd **LaunchAgent**,
  which `com.canopy.runner` is — not a LaunchDaemon).
- **Windows** — `WScript.Shell.Popup` via PowerShell, run under the interactive
  Scheduled Task (`\\Canopy\\canopy-runner`, registered for the logged-in user
  precisely so it has a desktop). Popup is the one native prompt that takes a
  TIMEOUT, which the contract below depends on; a WinForms MessageBox would hang
  forever with nobody there. Its button labels are fixed by the OS, so the
  Yes/No/Cancel triple is mapped to our three choices and the mapping is spelled
  out in the message body rather than left for the reader to guess.

Either way, if the dialog cannot render or times out with nobody answering, the
choice comes back as NEW — the non-destructive default (route to a fresh session,
leave the existing prompt untouched). We NEVER delete the human's text without an
explicit "Clear & send".
"""
from __future__ import annotations

import os as _os
import subprocess

from . import platform_jobs

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

# Windows counterpart. WScript.Shell.Popup(text, seconds, title, flags) is the only
# native prompt that self-times-out; flags 3 = Yes/No/Cancel, +32 = question icon.
# Returns 6=Yes, 7=No, 2=Cancel, and -1 when it gave up — the exact shape the
# AppleScript's `gave up of r` gives us, so both branches share one contract.
# The message text is passed on stdin, not interpolated, so a composer line
# carrying quotes or a `$` cannot break out into PowerShell.
_POWERSHELL = r"""
$msg = [Console]::In.ReadToEnd()
try {
  $w = New-Object -ComObject WScript.Shell
  $r = $w.Popup($msg, $env:CANOPY_DIALOG_TIMEOUT -as [int], "canopy runner - session busy", 3 + 32)
} catch { Write-Output "New session"; exit 0 }
switch ($r) {
  6       { Write-Output "Clear & send" }
  7       { Write-Output "New session" }
  2       { Write-Output "Cancel" }
  default { Write-Output "New session" }   # -1 = timed out, nobody there
}
"""

#: Windows MessageBox labels are the OS's, not ours, so the mapping has to be
#: visible in the body. macOS renders our three labels directly and needs no key.
_WINDOWS_KEY = (
    "\n\n[ Yes ] = Clear & send     "
    "[ No ] = New session     "
    "[ Cancel ] = do nothing"
)


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
    # `timeout` must be positive on BOTH renderers: AppleScript's `giving up after 0`
    # and Popup's 0 both mean "wait forever", which would wedge the runner's turn
    # loop behind a dialog nobody is looking at.
    timeout = max(1, int(timeout))
    if platform_jobs.is_windows():
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL]
        stdin, env = msg + _WINDOWS_KEY, {"CANOPY_DIALOG_TIMEOUT": str(timeout)}
    else:
        cmd = ["osascript", "-", msg, str(timeout)]
        stdin, env = _APPLESCRIPT, None
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            # Both renderers self-time-out at `timeout`; this is the backstop for a
            # renderer that hangs instead (no desktop, a wedged COM host).
            timeout=timeout + 10,
            **({"env": {**_os.environ, **env}} if env else {}),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return NEW
    choice = (proc.stdout or "").strip()
    return choice if choice in (CLEAR, NEW, CANCEL) else NEW
