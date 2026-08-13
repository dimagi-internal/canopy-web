"""Parsing a Claude Code permission dialog out of a rendered terminal.

Every fixture here was CAPTURED, not written: `claude` driven in a real PTY and
rendered through a terminal emulator, which is the same character grid xterm
produces for emdash — so what the runner reads over CDP is what these strings
contain. Verified by roundtrip on 2026-07-28: the dialog below was detected,
answered with a keystroke, and the file it asked about was actually deleted.
"""
import pathlib

from canopy_runner.menu import TAB, answer_keys, find_menu, find_menu_settled

# Verbatim, as a terminal renders it. Note the leading spaces, the box rule, and
# that option 2's text wraps concerns beyond the tool itself.
PERMISSION = """\
❯ Delete the file target.txt using the Bash tool (rm).
  Read 1 file, listed 1 directory (ctrl+o to expand)
⏺ Bash(rm /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-em
      dash-acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2/
      target.txt &&…)
  ⎿  Waiting…
────────────────────────────────────────────────────────────────────────────────
 Bash command
   rm /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-emd
   ash-acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2/t
   arget.txt && ls -la
   /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-emdash
   -acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2
   Delete target.txt and verify
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to rt2/ from this project
   3. No
 Esc to cancel · Tab to amend · ctrl+e to explain
"""

# The gate a fresh worktree hits first. A different dialog, same grammar — which
# is the point: one parser, or the runner grows a special case per dialog.
TRUST = """\
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:
 /private/tmp/scratchpad/menu-work
 Quick safety check: Is this a project you created or one you trust?
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""

# A working session. The single most important negative case: mistaking this for
# a menu would tell the phone an agent is blocked when it is busy, and every
# "needs you" after that is noise.
BUSY = """\
❯ Delete the file target.txt using the Bash tool (rm).
⏺ Bash(rm target.txt && ls -la)
  ⎿  Waiting…
✻ Sautéed for 6s
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
"""


def test_a_permission_dialog_is_found():
    menu = find_menu(PERMISSION)
    assert menu is not None
    assert menu.question == "Do you want to proceed?"


def test_every_option_is_extracted_in_order():
    menu = find_menu(PERMISSION)
    assert [(o.number, o.label) for o in menu.options] == [
        (1, "Yes"),
        (2, "Yes, and always allow access to rt2/ from this project"),
        (3, "No"),
    ]


def test_the_selected_option_is_known():
    """The ❯ marker is which choice is highlighted. Answering by NUMBER makes it
    cosmetic — but showing the wrong default on a phone is its own bug."""
    assert find_menu(PERMISSION).selected == 1


def test_the_subject_is_carried_so_the_phone_can_show_WHAT_it_is_asking():
    """A menu with no subject is unanswerable away from the keyboard: "Do you
    want to proceed?" tells you nothing about what you are approving."""
    menu = find_menu(PERMISSION)
    assert menu.title == "Bash command"
    assert "rm " in menu.body
    assert "target.txt" in menu.body


def test_a_wrapped_command_is_rejoined():
    """The TUI hard-wraps the command across grid lines mid-token
    ('…canopy-web-emd' / 'ash-acp-runner…'). Joining with a space or a newline
    corrupts the command being approved — which is the one string a human is
    reading to decide."""
    body = find_menu(PERMISSION).body
    assert "canopy-web-emdash-acp-runner-730f0" in body
    assert "canopy-web-emd ash" not in body


def test_the_trust_gate_parses_with_the_same_grammar():
    menu = find_menu(TRUST)
    assert menu is not None
    assert [(o.number, o.label) for o in menu.options] == [
        (1, "Yes, I trust this folder"),
        (2, "No, exit"),
    ]


def test_a_busy_session_is_not_a_menu():
    assert find_menu(BUSY) is None


def test_empty_and_garbage_are_not_menus():
    assert find_menu("") is None
    assert find_menu("just some output\nand more") is None


def test_numbered_prose_is_not_a_menu():
    """An agent writing a numbered list must not read as a dialog."""
    prose = """\
⏺ Here is the plan:
   1. Read the file
   2. Change the thing
   3. Run the tests
"""
    assert find_menu(prose) is None


def test_answering_is_the_number_then_enter():
    """Roundtrip-verified 2026-07-28: '1' + Enter on the dialog above ran the
    command and deleted the file."""
    assert answer_keys(1) == ["1", "\r"]
    assert answer_keys(3) == ["3", "\r"]


def test_refusing_is_escape_not_a_number():
    """Every dialog offers Esc, and it is the only answer that is safe when the
    option numbering is not what we think it is."""
    assert answer_keys(None) == ["\x1b"]


def test_an_option_outside_the_menu_is_refused():
    """A client could post any number; approving option 9 of a 3-option dialog
    by typing '9' would leave the prompt open and the turn wedged."""
    menu = find_menu(PERMISSION)
    assert menu.allows(1) and menu.allows(3)
    assert not menu.allows(4)
    assert not menu.allows(0)


def test_a_half_drawn_frame_is_retried_not_treated_as_no_dialog():
    """Observed live: a read caught the TUI mid-render — options painted, footer
    not yet — so the dialog did not parse and a real tap was dropped as stale.
    Failing safe is right; silently doing nothing is not."""
    frames = iter([
        " ❯ 1. Yes\n   2. No\n",          # mid-render: no footer yet
        PERMISSION,                        # settled
    ])
    found = find_menu_settled(lambda: next(frames), attempts=3, delay=0)
    assert found is not None
    assert found.question == "Do you want to proceed?"


def test_a_screen_with_no_dialog_still_resolves_to_none():
    assert find_menu_settled(lambda: BUSY, attempts=2, delay=0) is None


# The menu that actually appears in the fleet. Captured live 2026-07-28 from an
# emdash session running `⏵⏵ bypass permissions on` — where a PERMISSION dialog
# can never render, so this is the shape that matters in practice. Note what it
# does that the permission dialog does not: a description under each option, a
# box rule BETWEEN options 3 and 4, a question with no "?", and two trailing
# options Claude Code adds itself.
ASK_USER_QUESTION = """\
❯ Use the AskUserQuestion tool right now to ask me one question: "Pick a colour"
  nothing else and do nothing else first.
────────────────────────────────────────────────────────────────────────────────
 ☐ Colour
Pick a colour
❯ 1. Red
     The colour red.
  2. Blue
     The colour blue.
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def test_the_menu_the_fleet_actually_shows_parses():
    """Roundtrip-verified live: this dialog was detected off a real emdash
    terminal, answered with option 1, and the transcript recorded
    'Pick a colour'='Red' followed by the agent saying "You picked Red"."""
    menu = find_menu(ASK_USER_QUESTION)
    assert menu is not None
    assert menu.question == "Pick a colour"
    assert [(o.number, o.label) for o in menu.options] == [
        (1, "Red"), (2, "Blue"), (3, "Type something."), (4, "Chat about this"),
    ]
    assert menu.selected == 1


def test_option_descriptions_are_not_mistaken_for_options():
    """Each option carries an indented description line. Treating one as an
    option would shift every number, and the answer would select the wrong
    thing — silently, since a number always presses something."""
    menu = find_menu(ASK_USER_QUESTION)
    assert all("The colour" not in o.label for o in menu.options)


def test_a_rule_between_options_does_not_truncate_the_menu():
    """The TUI drew a box rule between options 3 and 4. Stopping at it would hide
    a real choice from the phone."""
    assert len(find_menu(ASK_USER_QUESTION).options) == 4


# Captured live 2026-08-01, over CDP, off a session that had been blocked for
# 1h20m with the phone showing nothing. Six options each carrying a description
# overflowed the pane, so the footer — the thing the parser required — was never
# drawn: the frame simply ends on the last option. Verbatim, including the
# trailing space after the title and the wrapped description lines.
FOOTERLESS = """\
────────────────────────────────────────────────────────────────────────────────
 ☐ Scope

How far do you want to take this?

❯ 1. Both, bulk_create first (Recommended)
     Fix the write path (chunked bulk_create + chunked POST), then make the runner persist every session eagerly so the server's copy is
     complete. 'Load full session' becomes a pure server read. Deletes 4 of the 5 audit findings.
  2. Fast only — fix the write path
     Chunked bulk_create + chunked backfill POST + client polls instead of guessing 1200ms + laptop runner handles the 'stream' frame. Keeps
     backfill on-demand; server stays at ~16% until asked. Much smaller diff.
  3. Accurate only — eager persist
     Runner persists every session it tracks, so the server converges to 100%.
  4. Just the audit for now
     Stop here. I write this up as an issue or spec against canopy-web and you decide later.
  5. Type something.
────────────────────────────────────────────────────────────────────────────────
  6. Chat about this
"""


def test_a_dialog_whose_footer_fell_off_the_frame_still_parses():
    """The regression this file exists to prevent, found in production twice on
    2026-08-01: a real dialog, unanswerable from a phone because the pane was too
    short to draw the footer the parser demanded."""
    menu = find_menu(FOOTERLESS)
    assert menu is not None
    assert menu.question == "How far do you want to take this?"
    assert menu.selected == 1
    assert [o.number for o in menu.options] == [1, 2, 3, 4, 5, 6]
    assert menu.options[3].label == "Just the audit for now"


def test_a_footerless_dialog_needs_the_selection_cursor():
    """The cursor is what separates the frame above from an agent's numbered
    list. Without it, a footerless block of numbers is prose."""
    assert find_menu(FOOTERLESS.replace("❯ 1.", "  1.")) is None


def test_a_menu_an_agent_QUOTED_is_not_a_menu():
    """This session pasted a live dialog — cursor and all — into a written
    report. A dialog is drawn where the composer would be, so nothing follows a
    real one; prose beneath it is what gives the quote away."""
    quoted = FOOTERLESS + "\n⏺ That is the dialog it is stuck on. Two options:\n"
    assert find_menu(quoted) is None


# The same bug's other half, captured live 2026-08-01 from a session blocked for
# 15 minutes. The question ran past the 140-column pane, so the TUI word-wrapped
# it — and "the last line ending in '?'" is then only its tail. Kept verbatim,
# trailing space and all: that space IS the wrap, and the parser keys on it.
WRAPPED_QUESTION = """\
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ☐ Unstick ada 

Ada is blocked on "Where does the auto-mode switch live, and what's its scope?" — the wire is working right now, so which answer should I 
press into that session?

❯ 1. Option 1 — Agent-repo config
  2. Option 3 — Both: config + override
  3. Option 2 — Per-dispatch flag
  4. Escape — cancel the dialog
  5. Type something.
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  6. Chat about this
"""


def test_a_word_wrapped_question_is_rejoined():
    """The phone was shown "press into that session?" — grammatical, useless, and
    indistinguishable from a genuinely short question."""
    menu = find_menu(WRAPPED_QUESTION)
    assert menu is not None
    assert menu.question.startswith("Ada is blocked on")
    assert menu.question.endswith("so which answer should I press into that session?")
    # The \u2610 is the tab strip's own chrome and now parses into `tabs`. The label
    # still reaches the phone as the title \u2014 a single-question ask has exactly
    # one answerable tab, and its header is the dialog's name.
    assert menu.tabs == ["Unstick ada"]
    assert menu.title == "Unstick ada"


def test_a_subject_line_is_never_swallowed_into_the_question():
    """A permission dialog's subject sits directly above its question and is a
    different thing. Only a line that ran to the frame's width and broke at a
    space is a continuation — this one is short, so it stays the subject."""
    menu = find_menu(PERMISSION)
    assert menu.question == "Do you want to proceed?"
    assert "Delete target.txt and verify" not in menu.question


# --- Tabbed / multi-select asks --------------------------------------------
#
# Captured the same way as everything above: `claude` driven in a real PTY with
# an AskUserQuestion carrying two questions, the first `multiSelect: true`, and
# the rendered grid taken verbatim at each step. The keystroke semantics these
# assert were established by pressing the keys and watching the screen, on
# 2026-08-12 — a number TOGGLES a checkbox, Tab moves tab, and the dialog is not
# submitted until the review tab's own button is pressed.

RULE = "─" * 150

TABBED_MULTI = f"""\
{RULE}
←  ☐ Colors  ☐ Size  ✔ Submit  →

Which colors do you want?

❯ 1. [ ] Red
  Warm and energetic
  2. [ ] Green
  Natural and balanced
  3. [ ] Blue
  Calm and trustworthy
  4. [ ] Type something
     Next
{RULE}
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

TABBED_MULTI_TWO_CHECKED = TABBED_MULTI.replace("1. [ ] Red", "1. [✔] Red").replace(
    "3. [ ] Blue", "3. [✔] Blue"
).replace("☐ Colors", "☒ Colors")

TABBED_SINGLE = f"""\
{RULE}
←  ☒ Colors  ☐ Size  ✔ Submit  →

Which size?

❯ 1. Small
     Compact and efficient
  2. Large
     Spacious and generous
  3. Type something.
{RULE}
  4. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

REVIEW = f"""\
{RULE}
←  ☒ Colors  ☒ Size  ✔ Submit  →

Review your answers

 ● Which colors do you want?
   → Red, Blue
 ● Which size?
   → Large

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel
"""

# One question, single-select: NO tab strip arrows and no Submit tab. This is the
# shape a lone number key has always finished, and it must keep working.
LONE_SINGLE = f"""\
{RULE}
 ☐ Size

Which size?

❯ 1. Small
     Compact option with minimal footprint.
  2. Large
     Expanded option with full capacity.
  3. Type something.
{RULE}
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

QUESTIONS = [
    {"index": 0, "question": "Which colors do you want?", "header": "Colors",
     "multi_select": True,
     "options": [{"number": 1, "label": "Red"}, {"number": 2, "label": "Green"},
                 {"number": 3, "label": "Blue"}]},
    {"index": 1, "question": "Which size?", "header": "Size", "multi_select": False,
     "options": [{"number": 1, "label": "Small"}, {"number": 2, "label": "Large"}]},
]


def test_a_checkbox_row_parses_its_box_separately_from_its_label():
    """"[ ] Red" reaching a phone as a button label is the string a human reads;
    the box state belongs in a field the driver can compare against."""
    menu = find_menu(TABBED_MULTI)
    assert menu is not None
    assert [o.label for o in menu.options][:3] == ["Red", "Green", "Blue"]
    assert [o.checked for o in menu.options][:3] == [False, False, False]
    assert menu.is_multi_select
    assert find_menu(TABBED_MULTI_TWO_CHECKED).checked_numbers() == [1, 3]


def test_the_tab_strip_parses_and_stays_out_of_the_title():
    menu = find_menu(TABBED_MULTI)
    assert menu.tabs == ["Colors", "Size", "Submit"]
    assert menu.needs_submit
    # Two answerable tabs: we cannot tell which is current from the strip, and a
    # title naming the wrong question is worse than none.
    assert menu.title == ""


def test_a_lone_single_select_needs_no_submit_step():
    """The one shape today's number+Enter actually finishes."""
    menu = find_menu(LONE_SINGLE)
    assert menu is not None
    assert menu.tabs == ["Size"]
    assert not menu.needs_submit
    assert not menu.is_multi_select
    assert menu.title == "Size"


def test_a_single_select_question_is_not_read_as_multi_select():
    menu = find_menu(TABBED_SINGLE)
    assert not menu.is_multi_select
    assert all(o.checked is None for o in menu.options)
    assert menu.needs_submit


def test_the_review_screen_is_recognised():
    menu = find_menu(REVIEW)
    assert menu is not None
    assert menu.is_review
    assert [o.label for o in menu.options] == ["Submit answers", "Cancel"]


# --- Driving ----------------------------------------------------------------


def test_it_presses_only_the_boxes_that_differ():
    """The whole idempotence story. A number TOGGLES, so replaying an answer that
    has already landed must press nothing rather than undo it — which is what
    makes the control-frame/poll-tick double delivery survivable."""
    from canopy_runner.menu import plan_step

    fresh = find_menu(TABBED_MULTI)
    assert plan_step(fresh, QUESTIONS, [[1, 3], [2]]) == ["1", "3"]

    already = find_menu(TABBED_MULTI_TWO_CHECKED)
    # Boxes already right: nothing to toggle, so move to the next tab.
    assert plan_step(already, QUESTIONS, [[1, 3], [2]]) == ["\t"]


def test_it_unchecks_a_box_the_answer_does_not_want():
    from canopy_runner.menu import plan_step

    menu = find_menu(TABBED_MULTI_TWO_CHECKED)
    assert plan_step(menu, QUESTIONS, [[1], [2]]) == ["3"]


def test_a_single_select_tab_is_answered_by_its_number_alone():
    """It auto-advances, so no Enter here — an Enter would land on whatever the
    next tab drew."""
    from canopy_runner.menu import plan_step

    menu = find_menu(TABBED_SINGLE)
    assert plan_step(menu, QUESTIONS, [[1, 3], [2]]) == ["2"]


def test_a_lone_single_select_still_gets_number_and_enter():
    from canopy_runner.menu import plan_step

    menu = find_menu(LONE_SINGLE)
    lone = [{"index": 0, "question": "Which size?", "multi_select": False,
             "options": [{"number": 1, "label": "Small"}]}]
    assert plan_step(menu, lone, [[2]]) == ["2", "\r"]


def test_the_review_screen_presses_submit():
    from canopy_runner.menu import plan_step

    assert plan_step(find_menu(REVIEW), QUESTIONS, [[1, 3], [2]]) == ["1", "\r"]


def test_an_unrecognised_question_presses_nothing():
    """A dialog that is not the one we were told about must cost a dropped tap,
    never a guessed keystroke."""
    from canopy_runner.menu import plan_step

    other = [{"index": 0, "question": "Something else entirely?",
              "multi_select": True, "options": [{"number": 1, "label": "x"}]}]
    assert plan_step(find_menu(TABBED_MULTI), other, [[1]]) is None


def test_every_control_key_the_driver_emits_is_named_for_the_real_transport():
    """REGRESSION, 2026-08-12. The answer path is delivered over CDP, where
    Playwright NAMES its keys and rejects a raw control character outright
    ("Unknown key: \\t"). The sidecar mapped Enter and Escape but not Tab, so
    answering a TABBED dialog died after its first tab — having already toggled
    checkboxes, leaving the ask half-filled and unsubmitted.

    It survived every test because the PTY harness writes the raw byte and a
    terminal interprets it. Only the real transport cares, so this asserts the
    two agree: every control character this module can emit must be named in the
    sidecar's map.
    """
    import re
    from pathlib import Path

    sidecar = (Path(__file__).resolve().parents[1]
               / "canopy_runner" / "cdp" / "emdash_control.mjs").read_text()
    named = re.search(r"const NAMED_KEYS = \{([^}]*)\}", sidecar)
    assert named, "the sidecar's key map moved — this test cannot see it any more"
    mapped = set(re.findall(r"'((?:\\u[0-9a-fA-F]{4}|\\.|[^'\\])+)'\s*:", named.group(1)))
    mapped = {k.encode().decode("unicode_escape") for k in mapped}

    from canopy_runner.menu import DOWN

    emitted = {TAB, "\r", DOWN} | {k for k in answer_keys(None)}
    control = {k for k in emitted if not k.isprintable() or k.startswith('\x1b')}
    assert control <= mapped, f"unmapped control keys: {control - mapped}"


# --- free text ("Type something") -------------------------------------------
#
# Recipe established against a live TUI 2026-08-12: pressing this row's number
# does NOT answer — it moves the cursor there and opens an edit field (the
# footer gains "ctrl+g to edit in Vim") — then you type and press Enter. The
# row's NUMBER is appended by the TUI and is absent from the tool input, so it
# can only be read off the render. Verified end to end: typing "Medium,
# actually" produced `⏺ GOT=Medium` from the agent.

LONE_SINGLE_TYPED = LONE_SINGLE.replace("3. Type something.", "3. Medium, actually")

TABBED_MULTI_TYPED = TABBED_MULTI_TWO_CHECKED.replace(
    "4. [ ] Type something", "4. [✔] Teal")


def test_the_type_something_row_is_found_by_label_not_by_counting():
    from canopy_runner.menu import type_something_number

    assert type_something_number(find_menu(LONE_SINGLE)) == 3
    assert type_something_number(find_menu(TABBED_MULTI)) == 4
    # Once filled in, the label IS the text, so the row is no longer offered.
    assert type_something_number(find_menu(LONE_SINGLE_TYPED)) is None


def test_free_text_walks_the_cursor_to_the_row_then_types():
    """The cursor is what receives typing, so it has to be WALKED there. A number
    key will not do it on a multi-select — there a number toggles a checkbox and
    leaves the cursor put, so the text lands on the wrong row (observed live: the
    label stayed "Type something", the previous tick was cleared, and the driver
    oscillated to its step cap)."""
    from canopy_runner.menu import DOWN, TEXT_PREFIX, plan_step

    lone = [{"index": 0, "question": "Which size?", "multi_select": False,
             "options": [{"number": 1, "label": "Small"}]}]
    step = plan_step(find_menu(LONE_SINGLE), lone, [[]], ["Medium, actually"])
    # Cursor on row 1, "Type something" is row 3. No trailing Enter: typing alone
    # fills the row in, and an Enter here cleared the earlier selection.
    # Single-select commits with Enter: the row BELOW "Type something" there is
    # "Chat about this", so a ↓ would park the cursor on something that opens a
    # chat. Verified live — this produced `GOT=Medium` from a real agent.
    assert step == [DOWN, DOWN, TEXT_PREFIX + "Medium, actually", "\r"]


def test_free_text_replaces_a_single_select_pick():
    """The TUI moves the selection onto the text row, so pressing a declared
    option as well would just overwrite what was typed."""
    from canopy_runner.menu import TEXT_PREFIX, plan_step

    lone = [{"index": 0, "question": "Which size?", "multi_select": False,
             "options": [{"number": 1, "label": "Small"}]}]
    step = plan_step(find_menu(LONE_SINGLE), lone, [[1]], ["Medium"])
    assert TEXT_PREFIX + "Medium" in step and "1" not in step


def test_text_already_entered_is_not_typed_again():
    """Same idempotence story as the checkboxes: this system delivers every
    answer twice, and re-typing would append to what is already there."""
    from canopy_runner.menu import plan_step

    lone = [{"index": 0, "question": "Which size?", "multi_select": False,
             "options": [{"number": 1, "label": "Small"}]}]
    # Entered. A lone dialog has no Submit tab, so it is confirmed with Enter —
    # exactly as picking an option would have been.
    # Committed already (a lone dialog has no Submit tab), so nothing is left.
    assert plan_step(find_menu(LONE_SINGLE_TYPED), lone, [[]], ["Medium, actually"]) is None


def test_a_multi_select_toggles_boxes_before_typing():
    """Typing moves focus to the text row; a number pressed from there edits the
    text instead of toggling a box."""
    from canopy_runner.menu import DOWN, TEXT_PREFIX, plan_step

    # Boxes not yet right -> toggles come first, text is not touched yet.
    assert plan_step(find_menu(TABBED_MULTI), QUESTIONS, [[1, 3], [2]], ["Teal", None]) == ["1", "3"]
    # Boxes right -> walk the cursor to row 4 and type. Verified against a live
    # multi-select: the row auto-ticks and takes the text as its label.
    step = plan_step(find_menu(TABBED_MULTI_TWO_CHECKED), QUESTIONS, [[1, 3], [2]], ["Teal", None])
    # Multi-select commits with ↓: the row below is the dialog's Next/Submit
    # action, so stepping off banks the text and leaves the cursor somewhere
    # the grid can show — no hidden edit state to guess at.
    # ↓ steps off the field onto the dialog's Next/Submit row; Enter activates
    # it. Tab from there does nothing at all (measured live).
    assert step == [DOWN, DOWN, DOWN, TEXT_PREFIX + "Teal", DOWN, "\r"]
    # Text entered too -> move on to the next tab.
    assert plan_step(find_menu(TABBED_MULTI_TYPED), QUESTIONS, [[1, 3], [2]], ["Teal", None]) == ["\t"]


def test_text_that_cannot_be_entered_presses_nothing():
    """The row is gone and nothing on screen carries the text, so typing would
    land somewhere unpredictable. Refusing costs the answer and says so."""
    from canopy_runner.menu import plan_step

    lone = [{"index": 0, "question": "Which size?", "multi_select": False,
             "options": [{"number": 1, "label": "Small"}]}]
    assert plan_step(find_menu(LONE_SINGLE_TYPED), lone, [[]], ["Something else"]) is None


def test_the_text_marker_is_distinguishable_from_a_keypress():
    """The transport tells them apart by this prefix, so it must never collide
    with a real key: keys are one character, or a control character."""
    from canopy_runner.menu import TEXT_PREFIX

    assert len(TEXT_PREFIX) > 1 and not TEXT_PREFIX.isdigit()
    sidecar = (pathlib.Path(__file__).resolve().parents[1]
               / "canopy_runner" / "cdp" / "emdash_control.mjs").read_text()
    assert f"'{TEXT_PREFIX}'" in sidecar, "the sidecar does not know the text marker"
