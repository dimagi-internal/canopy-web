"""The blocked-agent dialog, end to end across every seam.

The unit suites cover each hop on its own. This one runs the whole chain with
only the two ENDS faked — a real captured terminal screen going in, a real
keystroke list coming out — because every bug this feature can have lives in a
seam: the runner reading a screen, the hook payload shape, the server frame, the
client-visible data, and the answer travelling back to a keystroke.

The screen below was captured from a live `claude` on 2026-07-28 and answered
for real (the file it asked about was deleted), so what enters here is what a
terminal actually renders.
"""
from __future__ import annotations

import pytest

# Menu ANSWERING moved out of main.py into hooks.py (the hook-shaped half);
# menu.py stays the pure parser. See packages/canopy_runner/README.md § Layout.
from canopy_runner import hooks as runner_hooks
from canopy_runner.hook_listener import HookListener
from apps.canopy_sessions.stream_map import turn_event_to_frames

pytestmark = pytest.mark.django_db


# Verbatim from the live capture.
SCREEN = """\
❯ Delete the file target.txt using the Bash tool (rm).
  Read 1 file, listed 1 directory (ctrl+o to expand)
⏺ Bash(rm /private/tmp/scratchpad/rt2/target.txt &&…)
  ⎿  Waiting…
────────────────────────────────────────────────────────────────────────────────
 Bash command
   rm /private/tmp/scratchpad/rt2/target.txt && ls -la
   /private/tmp/scratchpad/rt2
   Delete target.txt and verify
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to rt2/ from this project
   3. No
 Esc to cancel · Tab to amend · ctrl+e to explain
"""

AFTER_ANSWER = """\
⏺ Bash(rm /private/tmp/scratchpad/rt2/target.txt &&…)
  ⎿  total 0
⏺ Deleted. target.txt is gone.
❯
  ⏸ manual mode on · ? for shortcuts
"""


class FakeEmdash:
    """emdash over CDP: a screen to read, and keystrokes we can assert on."""

    def __init__(self, screen):
        self.screen = screen
        self.sent = []

    def read_terminal(self, task, *, port=9222):
        return self.screen

    def send_keys(self, task, keys, *, port=9222):
        self.sent.append((task, keys))
        self.screen = AFTER_ANSWER      # the dialog is gone once answered
        return {"ok": True}


def _run_hook_to_frames(screen):
    """Notification hook -> what a browser receives. Every hop, no shortcuts."""
    emdash = FakeEmdash(screen)
    published: list = []

    listener = HookListener(
        port=0, nonce="n",
        resolve_session=lambda cwd: "session-1",
        forward=lambda: True,
        read_menu=lambda cwd: runner_hooks.read_hook_menu_from(emdash, "agent-task"),
    )
    listener.bind_sender(lambda sid, events: published.append((sid, events)))
    listener.handle_payload({"hook_event_name": "Notification", "cwd": "/w/x",
                             "message": "Claude needs your permission"})

    frames = []
    for _sid, events in published:
        for event in events:
            frames.extend(turn_event_to_frames(event, lambda seq: f"m:{seq}"))
    return emdash, frames


def test_a_dialog_reaches_the_browser_with_everything_needed_to_answer():
    _emdash, frames = _run_hook_to_frames(SCREEN)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["event"] == "session.activity"
    assert frame["data"]["state"] == "blocked"

    menu = frame["data"]["menu"]
    assert menu["question"] == "Do you want to proceed?"
    # The subject is what makes it answerable away from the keyboard.
    assert menu["title"] == "Bash command"
    assert "rm /private/tmp/scratchpad/rt2/target.txt" in menu["body"]
    assert [o["label"] for o in menu["options"]] == [
        "Yes",
        "Yes, and always allow access to rt2/ from this project",
        "No",
    ]


def test_a_busy_screen_reports_blocked_without_a_menu():
    """The state must survive even when there is nothing to render: losing the
    menu costs buttons, losing the state leaves you waiting on an agent that is
    waiting on you."""
    _emdash, frames = _run_hook_to_frames(AFTER_ANSWER)
    assert frames[0]["data"]["state"] == "blocked"
    assert "menu" not in frames[0]["data"]


def test_the_answer_becomes_the_keystrokes_that_dialog_expects():
    emdash, frames = _run_hook_to_frames(SCREEN)
    chosen = frames[0]["data"]["menu"]["options"][0]["number"]
    runner_hooks.answer_menu_with(emdash, "agent-task", chosen)
    assert emdash.sent == [("agent-task", ["1", "\r"])]


def test_refusing_sends_escape():
    emdash, _frames = _run_hook_to_frames(SCREEN)
    runner_hooks.answer_menu_with(emdash, "agent-task", None)
    assert emdash.sent == [("agent-task", ["\x1b"])]


def test_answering_twice_does_not_press_a_second_time():
    """The phone can double-tap, or two people can answer at once. The second
    answer finds no dialog on screen and must not type into the prompt — where
    the agent would read a bare '1' as an instruction."""
    emdash, frames = _run_hook_to_frames(SCREEN)
    runner_hooks.answer_menu_with(emdash, "agent-task", 1)
    runner_hooks.answer_menu_with(emdash, "agent-task", 1)
    assert emdash.sent == [("agent-task", ["1", "\r"])]


def test_an_option_the_dialog_does_not_offer_is_never_pressed():
    emdash, _frames = _run_hook_to_frames(SCREEN)
    runner_hooks.answer_menu_with(emdash, "agent-task", 9)
    assert emdash.sent == []
