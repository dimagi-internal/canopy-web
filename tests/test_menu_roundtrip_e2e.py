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
# menu.py stays the pure parser. See runner/canopy_runner/README.md § Layout.
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


# --- the tabbed / multi-select ask, across the same seams -------------------
#
# Captured from a live `claude` on 2026-08-12 driving a two-question
# AskUserQuestion with `multiSelect` on the first. The keystroke expectations
# below were verified against that real TUI, not reasoned about: a number
# TOGGLES a checkbox, Tab moves tab, a single-select tab auto-advances, and
# nothing is submitted until the review tab's own button is pressed.
#
# Until this shape was driven properly it was UNANSWERABLE from the web. The tap
# landed, toggled one box, and the dialog sat waiting on a Submit no phone could
# reach — eva's July closeout, blocked exactly this way.

_RULE = "─" * 120

TABBED_SCREENS = {
    "colors": f"""\
{_RULE}
←  ☐ Colors  ☐ Size  ✔ Submit  →

Which colors do you want?

❯ 1. [ ] Red
  2. [ ] Green
  3. [ ] Blue
  4. [ ] Type something
     Next
{_RULE}
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
""",
    "colors_done": f"""\
{_RULE}
←  ☒ Colors  ☐ Size  ✔ Submit  →

Which colors do you want?

❯ 1. [✔] Red
  2. [ ] Green
  3. [✔] Blue
  4. [ ] Type something
     Next
{_RULE}
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
""",
    "size": f"""\
{_RULE}
←  ☒ Colors  ☐ Size  ✔ Submit  →

Which size?

❯ 1. Small
  2. Large
  3. Type something.
{_RULE}
  4. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
""",
    "review": f"""\
{_RULE}
←  ☒ Colors  ☒ Size  ✔ Submit  →

Review your answers

 ● Which colors do you want?
   → Red, Blue
 ● Which size?
   → Large

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel
""",
    "gone": "⏺ GOT=Red, Blue|Large\n\n❯\n  ⏵⏵ bypass permissions on\n",
}

TABBED_INPUT = {
    "questions": [
        {"question": "Which colors do you want?", "header": "Colors",
         "multiSelect": True,
         "options": [{"label": "Red", "description": "warm"},
                     {"label": "Green", "description": "natural"},
                     {"label": "Blue", "description": "cool"}]},
        {"question": "Which size?", "header": "Size", "multiSelect": False,
         "options": [{"label": "Small", "description": "compact"},
                     {"label": "Large", "description": "roomy"}]},
    ]
}


class FakeTabbedEmdash:
    """A terminal that responds to keys the way the real TUI was observed to.

    Not a mock of our own expectations: each transition below was reproduced
    against a live `claude` before being written down.
    """

    def __init__(self):
        self.state = "colors"
        self.checked: set[int] = set()
        self.sent: list = []

    @property
    def screen(self):
        if self.state == "colors" and self.checked == {1, 3}:
            return TABBED_SCREENS["colors_done"]
        return TABBED_SCREENS[self.state]

    def read_terminal(self, task, *, port=9222):
        return self.screen

    def send_keys(self, task, keys, *, port=9222):
        self.sent.append((task, list(keys)))
        for key in keys:
            if self.state == "colors":
                if key == "\t":
                    self.state = "size"
                elif key.isdigit():
                    n = int(key)
                    self.checked ^= {n}          # a number TOGGLES
            elif self.state == "size":
                if key.isdigit():
                    self.state = "review"        # single-select auto-advances
                elif key == "\t":
                    self.state = "review"
            elif self.state == "review":
                if key == "1":
                    self.state = "gone"
        return {"ok": True}


def _hook_menu(tool_input):
    from canopy_transcript import menu_from_hook

    return menu_from_hook({"hook_event_name": "PreToolUse",
                           "tool_name": "AskUserQuestion",
                           "tool_input": tool_input})


def test_the_browser_receives_every_question_with_its_multi_select_flag():
    """The client renders its form from this. Carrying only question 1 is what
    made the ask unanswerable — no button on it can reach tab 2's Submit."""
    menu = _hook_menu(TABBED_INPUT)
    assert [q["header"] for q in menu["questions"]] == ["Colors", "Size"]
    assert [q["multi_select"] for q in menu["questions"]] == [True, False]
    # …and an older client still sees exactly what it saw before.
    assert menu["question"] == "Which colors do you want?"
    assert [o["label"] for o in menu["options"]] == ["Red", "Green", "Blue"]


def test_a_tabbed_multi_select_answer_becomes_the_right_keystrokes():
    emdash = FakeTabbedEmdash()
    questions = _hook_menu(TABBED_INPUT)["questions"]
    current = runner_hooks.menu.find_menu(emdash.screen)

    outcome, _screen = runner_hooks._drive_selections(
        emdash, "agent-task", current, questions, [[1, 3], [2]], 9222)

    assert outcome == runner_hooks.ANSWERED
    assert [keys for _task, keys in emdash.sent] == [["1", "3"], ["\t"], ["2"], ["1", "\r"]]
    assert emdash.state == "gone"


def test_replaying_the_same_answer_does_not_undo_it():
    """The system delivers every answer TWICE by design — control frame, then
    poll tick. On a surface whose number keys TOGGLE, a blind replay would
    un-check what the first delivery checked. Driving from the drawn state makes
    the second pass a no-op instead."""
    emdash = FakeTabbedEmdash()
    questions = _hook_menu(TABBED_INPUT)["questions"]

    # First delivery gets as far as filling in the colours, then stops.
    emdash.send_keys("agent-task", ["1", "3"])
    assert emdash.checked == {1, 3}
    emdash.sent.clear()

    # Second delivery, same answer, from whatever is now on screen.
    current = runner_hooks.menu.find_menu(emdash.screen)
    runner_hooks._drive_selections(
        emdash, "agent-task", current, questions, [[1, 3], [2]], 9222)

    assert emdash.checked == {1, 3}              # not toggled back off
    assert [keys for _t, keys in emdash.sent] == [["\t"], ["2"], ["1", "\r"]]


def test_a_dialog_that_is_not_the_declared_ask_is_never_guessed_at():
    emdash = FakeTabbedEmdash()
    current = runner_hooks.menu.find_menu(emdash.screen)
    other = [{"index": 0, "question": "Something else?", "multi_select": True,
              "options": [{"number": 1, "label": "x"}]}]

    outcome, _screen = runner_hooks._drive_selections(
        emdash, "agent-task", current, other, [[1]], 9222)

    assert outcome == runner_hooks.UNMODELLED
    assert emdash.sent == []


def test_the_schema_accepts_the_exact_body_the_browser_sends():
    """REGRESSION, 2026-08-13. `texts` was typed `list[str]`, so a PARTIAL answer
    — the whole point of the partial-submit change — was rejected 422 by the API
    the moment it reached the live site. The list is POSITIONAL against the
    declared questions, so a hole has to be expressible: without None the runner
    would have to guess which question a shorter list belonged to.

    Captured verbatim from a browser POST.
    """
    from apps.canopy_sessions.schemas import MenuAnswerIn

    payload = MenuAnswerIn(**{"option": 1, "selections": [[1], []],
                              "texts": ["Teal", None]})
    assert payload.texts == ["Teal", None]
    assert payload.selections == [[1], []]
